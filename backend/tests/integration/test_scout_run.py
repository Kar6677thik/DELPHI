"""``RUN_SCOUT`` on a real database (docs/11 §2, FR-SCT-02, #117): a queue holding one answer
pattern becomes one cluster with a CANDIDATE typology draft, and the auto-trigger enqueues a run
once enough new items are waiting."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update

from delphi.guardian.api import attributed_sessions_query
from delphi.guardian.store import ProbeMode, add_probe_run
from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.kb.store import active_version
from delphi.platform.jobs import JobContext, JobType, enqueue
from delphi.scout.api import queue_items, run_scout, scout_clusters, typology_drafts
from delphi.scout.run import ScoutAutoTriggerJob
from delphi.scout.tables import scout_runs

from .test_cases import T0, Env, env  # noqa: F401 - pytest fixture
from .test_scout_queue import _decided

# The collect-refund pattern: every victim believes they are receiving money (docs/14 §5.4).
PATTERN = {
    "CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER",
    "PURPOSE_QUESTION": "BELIEVES_RECEIVING_MONEY",
    "COLLECT_INTENT_CHECK": "EXPECTS_TO_RECEIVE",
}


def _with_outcomes(e: Env, session_id: UUID, outcomes: dict[str, str]) -> None:
    with e.sessions.begin() as s:
        for key in outcomes:
            add_probe_run(s, e.clock, session_id, probe_key=key, mode=ProbeMode.PASSIVE)
    with e.owner.begin() as conn:
        for key, outcome in outcomes.items():
            conn.execute(
                update(probe_runs)
                .where(probe_runs.c.session_id == session_id, probe_runs.c.probe_key == key)
                .values(status="COMPLETED", outcome=outcome, completed_at=T0)
            )


def _kb(e: Env) -> Any:
    with e.sessions() as s:
        version = active_version(s)
        assert version is not None
        return version.kb


def test_one_pattern_is_one_cluster_with_a_candidate_draft(env: Env) -> None:  # noqa: F811
    members = []
    for minute in range(6):
        session_id = _decided(env, unexplained=True, minutes=minute)
        _with_outcomes(env, session_id, PATTERN)
        members.append(session_id)
    env.clock.advance(timedelta(hours=1))
    with env.sessions.begin() as s:
        run_id = run_scout(s, env.clock, _kb(env), max_items=500)
    with env.owner.connect() as conn:
        [cluster] = conn.execute(
            select(scout_clusters).where(scout_clusters.c.run_id == run_id)
        ).all()
        [draft] = conn.execute(
            select(typology_drafts).where(typology_drafts.c.cluster_id == cluster.id)
        ).all()
        run = conn.execute(text("SELECT n_items, n_clusters, params FROM delphi.scout_runs")).one()
    assert cluster.size == 6
    assert {UUID(r["subject_id"]) for r in cluster.member_refs} == set(members)
    assert cluster.distinguishing_features["first_at"] < cluster.distinguishing_features["last_at"]
    assert cluster.outcome_profile["PURPOSE_QUESTION"]["BELIEVES_RECEIVING_MONEY"] == 1.0
    assert draft.status == "CANDIDATE" and draft.revision == 1
    assert draft.spec["name"].startswith("Candidate pattern 1")
    row = draft.spec["likelihoods"]["PURPOSE_QUESTION"]
    assert max(row["probs"], key=float) == row["probs"][3]  # BELIEVES_RECEIVING_MONEY
    assert run.n_items == 6 and run.n_clusters == 1
    assert run.params["cluster_selection_epsilon"] == 1.0
    # A candidate assigns nothing: the members stay in the queue until it is promoted.
    with env.sessions() as s:
        assert len(queue_items(s, _kb(env), limit=200)) == 6


def test_the_auto_trigger_waits_for_enough_new_items(env: Env) -> None:  # noqa: F811
    job = ScoutAutoTriggerJob(env.sessions, env.clock)
    ctx: Any = JobContext.__new__(JobContext)
    ctx.job = type("J", (), {"id": UUID(int=1)})()
    for minute in range(9):
        _decided(env, unexplained=True, minutes=minute)
    assert job(ctx) == {"enqueued": False, "new_items": 9}
    _decided(env, unexplained=True, minutes=10)
    assert job(ctx) == {"enqueued": True, "new_items": 10}
    # While that run is QUEUED (or RUNNING), later ticks enqueue nothing more (#255).
    _decided(env, unexplained=True, minutes=11)
    assert job(ctx) == {"enqueued": False, "reason": "a run is pending"}
    with env.owner.connect() as conn:
        queued = conn.execute(
            text("SELECT count(*) FROM delphi.jobs WHERE type = 'RUN_SCOUT'")
        ).scalar_one()
    assert queued == 1


def test_a_retried_run_returns_the_jobs_run(env: Env) -> None:  # noqa: F811
    """docs/04 §5.4: RUN_SCOUT is idempotent. A retry after a lost lease (the same job) finds
    the run the first attempt stored, and stores no second one."""
    for minute in range(6):
        _with_outcomes(env, _decided(env, unexplained=True, minutes=minute), PATTERN)
    with env.sessions.begin() as s:
        job = enqueue(s, env.clock, JobType.RUN_SCOUT, idempotency_key="t", params={})
    with env.sessions.begin() as s:
        first = run_scout(s, env.clock, _kb(env), max_items=500, job_id=job.job_id)
    with env.sessions.begin() as s:
        again = run_scout(s, env.clock, _kb(env), max_items=500, job_id=job.job_id)
    assert again == first
    with env.owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM delphi.scout_runs")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM delphi.scout_clusters")).scalar_one() == 1


def test_a_run_loads_at_most_max_items(env: Env) -> None:  # noqa: F811
    for minute in range(12):
        _with_outcomes(env, _decided(env, unexplained=True, minutes=minute), PATTERN)
    with env.sessions.begin() as s:
        run_id = run_scout(s, env.clock, _kb(env), max_items=7)
    with env.owner.connect() as conn:
        run = conn.execute(
            select(scout_runs.c.n_items, scout_runs.c.params).where(scout_runs.c.id == run_id)
        ).one()
    assert run.n_items == 7 and run.params["max_items"] == 7


def test_the_reference_set_is_the_attributed_sessions_by_sql_argmax(env: Env) -> None:  # noqa: F811
    """docs/11 §2: the argmax, taken in SQL, must be a known typology (not GENUINE, not
    UNEXPLAINED), and the session not flagged unexplained."""
    posteriors = {
        "da": {"GENUINE": 0.2, "APP_DIGITAL_ARREST": 0.7, "UNEXPLAINED": 0.1},
        "genuine": {"GENUINE": 0.8, "APP_DIGITAL_ARREST": 0.1, "UNEXPLAINED": 0.1},
        "open": {"GENUINE": 0.2, "APP_DIGITAL_ARREST": 0.3, "UNEXPLAINED": 0.5},
    }
    ids = {}
    for name, posterior in posteriors.items():
        ids[name] = _decided(env, unexplained=False)
        with env.owner.begin() as conn:
            conn.execute(
                update(guardian_sessions)
                .where(guardian_sessions.c.id == ids[name])
                .values(posterior=posterior)
            )
    query = attributed_sessions_query().subquery()
    with env.sessions() as s:
        found = set(s.execute(select(query.c.subject_id)).scalars())
    assert found == {ids["da"]}
