"""``RUN_SCOUT`` on a real database (docs/11 §2, FR-SCT-02, #117): a queue holding one answer
pattern becomes one cluster with a CANDIDATE typology draft, and the auto-trigger enqueues a run
once enough new items are waiting."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update

from delphi.guardian.store import ProbeMode, add_probe_run
from delphi.guardian.tables import probe_runs
from delphi.kb.store import active_version
from delphi.platform.jobs import JobContext
from delphi.scout.api import queue_items, run_scout, scout_clusters, typology_drafts
from delphi.scout.run import ScoutAutoTriggerJob

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
        run_id = run_scout(s, env.clock, _kb(env))
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
        assert len(queue_items(s, _kb(env))) == 6


def test_the_auto_trigger_waits_for_enough_new_items(env: Env) -> None:  # noqa: F811
    job = ScoutAutoTriggerJob(env.sessions, env.clock)
    ctx: Any = JobContext.__new__(JobContext)
    ctx.job = type("J", (), {"id": UUID(int=1)})()
    for minute in range(9):
        _decided(env, unexplained=True, minutes=minute)
    assert job(ctx) == {"enqueued": False, "new_items": 9}
    _decided(env, unexplained=True, minutes=10)
    assert job(ctx) == {"enqueued": True, "new_items": 10}
    with env.owner.connect() as conn:
        queued = conn.execute(
            text("SELECT count(*) FROM delphi.jobs WHERE type = 'RUN_SCOUT'")
        ).scalar_one()
    assert queued == 1
