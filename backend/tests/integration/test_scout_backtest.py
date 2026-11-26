"""``RUN_BACKTEST`` on a real database (docs/11 §4, FR-SCT-04, #118): a draft is replayed over the
recent sessions under ACTIVE and ACTIVE + the draft, and its report is attached to the draft,
which becomes BACKTESTED; a report for a revision edited since is never written."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from delphi.guardian.tables import guardian_sessions
from delphi.platform.jobs import JobContext
from delphi.scout.api import typology_drafts
from delphi.scout.backtest import LIMITATION, RunBacktestJob, replay

from .test_cases import BROWSER, Env, env, writer  # noqa: F401 - fixtures
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


def _label(e: Env, payment: UUID, hypothesis: str) -> None:
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delphi.labels (id, subject_type, subject_id, hypothesis_key,"
                " label_source, known_at) VALUES (gen_random_uuid(), 'PAYMENT', :s, :h,"
                " 'CUSTOMER_COMPLAINT', now())"
            ),
            {"s": payment, "h": hypothesis},
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
        params = conn.execute(
            text("SELECT params FROM delphi.jobs WHERE type = 'RUN_BACKTEST'")
        ).scalar_one()
    assert params == {"draft_id": str(candidate), "revision": 2}

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
    from delphi.kb.store import active_version

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
