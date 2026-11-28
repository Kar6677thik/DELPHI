"""``RUN_BACKTEST`` on a real database (docs/11 §4, FR-SCT-04, #118): a draft is replayed over the
recent sessions under ACTIVE and ACTIVE + the draft, and its report is attached to the draft,
which becomes BACKTESTED; a report for a revision edited since is never written."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text, update

from delphi.guardian.tables import guardian_sessions
from delphi.kb.model import KnowledgeBase
from delphi.kb.store import active_version
from delphi.navigator.api import open_case
from delphi.platform.jobs import JobContext
from delphi.platform.locks import BACKTEST_LOCK_KEY
from delphi.scout import backtest as bt
from delphi.scout.api import typology_drafts
from delphi.scout.backtest import (
    LIMITATION,
    BacktestBusyError,
    Item,
    Replayed,
    RunBacktestJob,
    replay,
)

from .test_cases import BROWSER, Env, _investigator, env, writer  # noqa: F401 - fixtures
from .test_scout_drafts import DRAFTS, VALID, _patch, candidate, fde  # noqa: F401 - fixtures

# p_model as a live session stores it (the payment classes with the payer's mule score,
# `with_payer_mule`), evenly, with a high anomaly percentile: the answers decide what leads.
P_MODEL = {
    "GENUINE": 0.2,
    "ATO": 0.2,
    "APP_DIGITAL_ARREST": 0.2,
    "APP_INVESTMENT": 0.2,
    "MULE": 0.2,
}
Q = 0.99


def _store_priors(e: Env) -> list[UUID]:
    with e.owner.begin() as conn:
        conn.execute(update(guardian_sessions).values(p_model=P_MODEL, anomaly_percentile=Q))
        return list(conn.execute(select(guardian_sessions.c.payment_id)).scalars())


def _label(
    e: Env, payment: UUID, hypothesis: str, source: str = "CUSTOMER_COMPLAINT", days: int = 0
) -> None:
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delphi.labels (id, subject_type, subject_id, hypothesis_key,"
                " label_source, known_at) VALUES (gen_random_uuid(), 'PAYMENT', :s, :h,"
                " :src, :at)"
            ),
            {"s": payment, "h": hypothesis, "src": source, "at": e.clock.now() + timedelta(days)},
        )


def _ctx(params: dict[str, Any]) -> Any:
    ctx: Any = JobContext.__new__(JobContext)
    ctx.job = type("J", (), {"id": UUID(int=1), "params": params})()
    return ctx


def _row(e: Env) -> Any:
    with e.owner.connect() as conn:
        return conn.execute(select(typology_drafts)).one()


def test_a_backtest_reports_and_marks_the_draft_backtested(
    env: Env,  # noqa: F811
    fde: TestClient,  # noqa: F811
    candidate: UUID,  # noqa: F811
) -> None:
    payments = _store_priors(env)
    _label(env, payments[0], "APP_COLLECT_REFUND")  # a fraud label on an attributed item
    # A CANDIDATE is not backtested: it is saved as a DRAFT first.
    url = f"{DRAFTS}/{candidate}/backtest"
    refused = fde.post(url, headers={**BROWSER, "Idempotency-Key": str(uuid4())})
    assert refused.status_code == 409
    assert _patch(fde, candidate, VALID, '"1"').status_code == 200
    queued = fde.post(url, headers={**BROWSER, "Idempotency-Key": str(uuid4())})
    assert queued.status_code == 202 and queued.json()["created"] is True
    again = fde.post(url, headers={**BROWSER, "Idempotency-Key": str(uuid4())})
    assert again.json() == {**queued.json(), "created": False}  # one job per revision
    with env.owner.connect() as conn:
        row = conn.execute(
            text("SELECT params, max_attempts FROM delphi.jobs WHERE type = 'RUN_BACKTEST'")
        ).one()
    params, max_attempts = row
    assert params == {"draft_id": str(candidate), "revision": 2}
    # A busy lock is a failed attempt, so it gets more than the platform's 3 (#300 review).
    assert max_attempts == 6  # DELPHI_SCOUT_BACKTEST_MAX_ATTEMPTS

    result = RunBacktestJob(env.sessions, env.clock, max_items=500)(_ctx(params))
    assert result == {"draft_id": str(candidate), "revision": 2, "written": True}
    row = _row(env)
    assert row.status == "BACKTESTED" and row.backtested_revision == 2 and row.version == 3
    report = row.backtest_report
    assert report["sessions"] == 6 and report["skipped"] == 0
    assert report["new_hypothesis"] == "APP_COLLECT_REFUND"
    assert report["revision"] == 2 and report["limitations"] == [LIMITATION]
    # Under ACTIVE the six queue items lead with UNEXPLAINED; the draft explains all six. The
    # decision stays a protective hold either way.
    assert report["unexplained_rate"]["before"] == 1.0
    assert report["unexplained_rate"]["after"] == 0.0
    assert report["attributed"] == 6
    assert report["attributed_labelled"] == 1 and report["attributed_labelled_precision"] == 1.0
    assert report["decision_changes"] == {"PROTECTIVE_HOLD→PROTECTIVE_HOLD": 6}
    assert report["genuine_disturbed"] == 0
    view = fde.get(f"{DRAFTS}/{candidate}").json()
    assert view["status"] == "BACKTESTED" and view["backtest_report"]["attributed"] == 6


def test_a_report_for_an_edited_revision_is_not_written(
    env: Env,  # noqa: F811
    fde: TestClient,  # noqa: F811
    candidate: UUID,  # noqa: F811
) -> None:
    _store_priors(env)
    assert _patch(fde, candidate, VALID, '"1"').status_code == 200
    assert _patch(fde, candidate, {"description": "edited"}, '"2"').status_code == 200  # rev 3
    result = RunBacktestJob(env.sessions, env.clock, max_items=500)(
        _ctx({"draft_id": str(candidate), "revision": 2})
    )
    assert result == {"skipped": "the draft is at revision 3 now"}
    row = _row(env)
    assert row.status == "DRAFT" and row.backtest_report is None


def test_the_replay_rebuilds_the_prior_and_applies_the_observations(env: Env) -> None:  # noqa: F811
    from delphi.engine import compile_profile

    with env.sessions() as s:
        version = active_version(s)
        assert version is not None
    profile = compile_profile(version.kb, "GUARDIAN_PAYMENT")
    alone = replay(profile, P_MODEL, Q, [], amount_paise=100_000, vulnerable=False)
    assert alone.argmax == "UNEXPLAINED"
    skipped = replay(
        profile, P_MODEL, Q, [("NO_SUCH_SOURCE", "X")], amount_paise=100_000, vulnerable=False
    )
    assert skipped == alone  # an observation the profile does not know is skipped


def _post(fde: TestClient, url: str) -> dict[str, Any]:  # noqa: F811
    reply = fde.post(url, headers={**BROWSER, "Idempotency-Key": str(uuid4())})
    assert reply.status_code == 202, reply.text
    return dict(reply.json())


def _set_job(e: Env, job_id: str, status: str, result: dict[str, Any] | None) -> None:
    with e.owner.begin() as conn:
        conn.execute(
            text("UPDATE delphi.jobs SET status = :s, result = CAST(:r AS jsonb) WHERE id = :i"),
            {
                "s": status,
                "r": None if result is None else __import__("json").dumps(result),
                "i": job_id,
            },
        )


def test_a_failed_or_skipped_backtest_can_run_again(
    env: Env,  # noqa: F811
    fde: TestClient,  # noqa: F811
    candidate: UUID,  # noqa: F811
) -> None:
    """#300 review: a revision's FAILED, cancelled or skipped run doesn't block it for good."""
    assert _patch(fde, candidate, VALID, '"1"').status_code == 200
    url = f"{DRAFTS}/{candidate}/backtest"
    first = _post(fde, url)
    assert first["created"] is True and first["status"] == "QUEUED"
    assert _post(fde, url) == {**first, "created": False}  # still queued: the same job
    _set_job(env, first["job_id"], "FAILED", None)
    second = _post(fde, url)
    assert second["created"] is True and second["job_id"] != first["job_id"]
    _set_job(env, second["job_id"], "SUCCEEDED", {"skipped": "no ACTIVE KB version"})
    third = _post(fde, url)
    assert third["created"] is True and third["job_id"] != second["job_id"]
    _set_job(env, third["job_id"], "SUCCEEDED", {"written": True})
    done = _post(fde, url)  # a report was written: asking again returns that run
    assert done == {"job_id": third["job_id"], "created": False, "status": "SUCCEEDED"}
    with env.owner.connect() as conn:
        keys = conn.execute(
            text("SELECT idempotency_key FROM delphi.jobs WHERE type = 'RUN_BACKTEST' ORDER BY id")
        ).scalars()
        # Under the fixed clock the jobs share a millisecond, so their ids don't sort by time.
        assert {k.removeprefix(f"RUN_BACKTEST:{candidate}:") for k in keys} == {"2", "2:2", "2:3"}


def test_a_busy_backtest_raises_so_the_job_is_retried(
    env: Env,  # noqa: F811
    fde: TestClient,  # noqa: F811
    candidate: UUID,  # noqa: F811
) -> None:
    """Lock 57: one replay at a time; a second one is retried later, not skipped."""
    _store_priors(env)
    assert _patch(fde, candidate, VALID, '"1"').status_code == 200
    job = RunBacktestJob(env.sessions, env.clock, max_items=500)
    params = {"draft_id": str(candidate), "revision": 2}
    with env.owner.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": BACKTEST_LOCK_KEY})
        with pytest.raises(BacktestBusyError):
            job(_ctx(params))
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BACKTEST_LOCK_KEY})
    assert _row(env).status == "DRAFT"
    assert job(_ctx(params))["written"] is True


def test_labels_follow_the_precedence_not_the_latest(env: Env) -> None:  # noqa: F811
    """docs/05 §8: an investigator's GENUINE outranks a registry listing known days later."""
    payment = env.payment()
    _label(env, payment, "GENUINE", "INVESTIGATOR_DISPOSITION", days=2)
    _label(env, payment, "MULE", "REGISTRY_LISTING", days=5)
    other = env.payment()
    _label(env, other, "APP_DIGITAL_ARREST", "CUSTOMER_COMPLAINT", days=1)
    _label(env, other, "GENUINE", "NO_COMPLAINT_60D", days=60)
    with env.sessions() as s:
        found = bt._labels(s, [("PAYMENT", payment), ("PAYMENT", other), ("ACCOUNT", payment)])
    assert found == {("PAYMENT", payment): "GENUINE", ("PAYMENT", other): "APP_DIGITAL_ARREST"}


def test_items_are_capped_take_cases_and_leave_dismissed_ones_out_of_the_queue(
    env: Env,  # noqa: F811
    candidate: UUID,  # noqa: F811
) -> None:
    _store_priors(env)
    with env.owner.connect() as conn:
        sessions = list(
            conn.execute(
                select(guardian_sessions.c.id, guardian_sessions.c.payment_id).order_by(
                    guardian_sessions.c.decided_at.desc(), guardian_sessions.c.id.desc()
                )
            )
        )
    newest = sessions[0]
    with env.sessions.begin() as s:
        # A case on the newest session's payment with its stored prior, and one with none.
        bare = env.payment()
        for payment, session_id, prior in (
            (newest.payment_id, newest.id, {"p_model": P_MODEL, "anomaly_percentile": Q}),
            (bare, env.new_session(bare), {"p_model": {}}),
        ):
            open_case(
                s,
                env.clock,
                decision="PROTECTIVE_HOLD",
                payment_id=payment,
                guardian_session_id=session_id,
                kb_version_id=env.kb_version_id,
                prior_components=prior,
                unexplained=True,
                posterior={"UNEXPLAINED": 0.6, "GENUINE": 0.4},
                amount_at_stake_paise=100_000,
            )
    with env.owner.begin() as conn:  # the newest session is dismissed from the queue
        conn.execute(
            text(
                "INSERT INTO delphi.scout_queue_dismissals (subject_type, subject_id, reason,"
                " dismissed_by, dismissed_at) VALUES ('GUARDIAN_SESSION', :i, 'known', :u, now())"
            ),
            {"i": newest.id, "u": _investigator(env).user_id},
        )
    with env.sessions() as s:
        items = bt.load_items(s, env.clock, days=30, max_items=2)
    kinds = [i.kind for i in items]
    assert kinds.count("session") == 2  # the cap, newest first
    assert kinds.count("case") == 1  # the case without a stored prior is filtered in SQL
    first = items[0]
    assert first.label_subject == ("PAYMENT", newest.payment_id) and first.in_queue is False
    case = next(i for i in items if i.kind == "case")
    # A payment case replays its session's probe runs, then its evidence.
    assert list(case.observations[: len(first.observations)]) == list(first.observations)


def test_genuine_disturbed_counts_a_genuine_session_whose_decision_changes(
    kb: KnowledgeBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_kb = kb.model_copy()
    monkeypatch.setattr(bt, "compile_profile", lambda which, _key: which)
    monkeypatch.setattr(
        bt,
        "replay",
        lambda profile, *_a, **_k: (
            Replayed("PROTECTIVE_HOLD", "NEW_KEY")
            if profile is draft_kb
            else Replayed("RELEASE", "UNEXPLAINED")
        ),
    )

    def item(subject: UUID, in_queue: bool) -> Item:
        return Item(
            "session", "GUARDIAN_PAYMENT", {}, 0.9, [], 1, False, in_queue, ("PAYMENT", subject)
        )

    genuine, fraud, unlabelled = uuid4(), uuid4(), uuid4()
    report = bt.report(
        kb,
        draft_kb,
        "NEW_KEY",
        [item(genuine, False), item(fraud, True), item(unlabelled, True)],
        {("PAYMENT", genuine): "GENUINE", ("PAYMENT", fraud): "APP_DIGITAL_ARREST"},
    )
    assert report["genuine_disturbed"] == 1
    assert report["attributed"] == 2 and report["attributed_labelled"] == 1
    assert report["attributed_labelled_precision"] == 1.0
    assert report["decision_changes"] == {"RELEASE→PROTECTIVE_HOLD": 3}
    assert report["unexplained_rate"] == {"before": 1.0, "after": 0.0}


def test_the_loads_use_their_indexes(env: Env, candidate: UUID) -> None:  # noqa: F811
    """#300 review: each session branch is served by one of 0020_scout's partial indexes, and
    the labels by ``(subject_type, subject_id, label_source)``. Captured from the real calls,
    then EXPLAINed with sequential scans off."""
    _store_priors(env)
    statements: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, params: Any, *_a: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, params))

    engine = env.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with env.sessions() as s:
            items = bt.load_items(s, env.clock, days=30, max_items=50)
            bt._labels(s, [i.label_subject for i in items if i.label_subject])
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    def plan(statement: str, params: Any) -> str:
        with env.owner.begin() as conn:
            conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
            rows = conn.exec_driver_sql(f"EXPLAIN {statement}", params)
            return "\n".join(r[0] for r in rows)

    sessions = [p for p in statements if "FROM delphi.guardian_sessions" in p[0]]
    plans = [plan(*p) for p in sessions]
    assert any("guardian_sessions_unexplained_decided_idx" in p for p in plans), plans
    assert any("guardian_sessions_reference_decided_idx" in p for p in plans), plans
    label = next(p for p in statements if "FROM delphi.labels" in p[0])
    assert "labels_subject_source_key" in plan(*label)
