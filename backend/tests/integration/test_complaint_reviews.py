"""The complaints trigger on a real database (docs/09 §1, FR-NAV-01, #109): a complaint naming a
home-bank account opens one ACCOUNT_REVIEW case with the mule-scorer prior updated with
LINKED_COMPLAINTS = ONE_OR_MORE, idempotent per account."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from delphi.engine.belief import update as bayes_update
from delphi.engine.prior import build_prior
from delphi.kb.loader import load_and_validate
from delphi.navigator.api import CaseProtocol, CaseStatus, CaseType, case_profile, current_records
from delphi.navigator.reviews import ComplaintSweepJob, open_complaint_reviews
from delphi.navigator.tables import cases
from delphi.platform.jobs import JobContext
from delphi.platform.tables import audit_log, outbox_events
from delphi.probes.registry import CLASSIFIER_NAMES

from .test_guardian_store import KB_DIR, T0, Env, env  # noqa: F401 - pytest fixture

FALLBACK = 0.10


def _account(e: Env, *, bank_code: str = "DLPH") -> UUID:
    account = uuid4()
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bank.accounts (id, customer_id, bank_code, opened_at, kyc_level,"
                " status, balance_paise) VALUES (:a, NULL, :b, :t, 'FULL', 'ACTIVE', 0)"
            ),
            {"a": account, "b": bank_code, "t": T0 - timedelta(days=90)},
        )
    return account


def _complaint(e: Env, *accounts: UUID, minutes_ago: int = 10) -> None:
    payment = e.payment()
    with e.owner.begin() as conn:
        victim = conn.execute(
            text(
                "SELECT a.customer_id FROM bank.payments p"
                " JOIN bank.accounts a ON a.id = p.payer_account_id WHERE p.id = :p"
            ),
            {"p": payment},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO bank.complaints (id, reported_at, victim_customer_id, channel,"
                " category, payment_ids, counterparty_account_ids)"
                " VALUES (:i, :t, :v, 'BRANCH', 'INVESTMENT_SCAM', ARRAY[:p]::uuid[],"
                " CAST(:a AS uuid[]))"
            ),
            {
                "i": uuid4(),
                "t": e.clock.now() - timedelta(minutes=minutes_ago),
                "v": victim,
                "p": payment,
                "a": "{" + ",".join(str(a) for a in accounts) + "}",
            },
        )


def _credit(e: Env, account: UUID, paise: int, *, days_ago: float) -> None:
    payer = _account(e)
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bank.payments (id, payer_account_id, payee_account_id, amount_paise,"
                " channel, initiated_at, status, origin, executed_at)"
                " VALUES (:i, :a, :b, :m, 'IMPS', :t, 'EXECUTED', 'SIM_LIVE', :t)"
            ),
            {
                "i": uuid4(),
                "a": payer,
                "b": account,
                "m": paise,
                "t": e.clock.now() - timedelta(days=days_ago),
            },
        )


def _sweep(e: Env) -> list[Any]:
    with e.sessions.begin() as s:
        return open_complaint_reviews(
            s, e.clock, kb_version_id=e.kb_version_id, p_mule_fallback=FALLBACK
        )


def _reviews(e: Env) -> int:
    with e.owner.connect() as conn:
        return int(
            conn.execute(
                select(func.count()).where(cases.c.type == CaseType.ACCOUNT_REVIEW.value)
            ).scalar_one()
        )


def _expected_posterior(p_mule: float) -> dict[str, float]:
    """docs/09 §1 by hand: the mule-scorer prior (q = 0), then LINKED_COMPLAINTS = ONE_OR_MORE."""
    kb = load_and_validate(KB_DIR, CLASSIFIER_NAMES)
    profile = case_profile(kb, CaseType.ACCOUNT_REVIEW.value)
    prior = build_prior(profile, {"MULE": p_mule, "GENUINE": 1 - p_mule}, 0.0)
    source = profile.sources["LINKED_COMPLAINTS"]
    after = bayes_update(prior, source.likelihood[source.outcomes.index("ONE_OR_MORE")])
    return {h: float(p) for h, p in zip(profile.hypotheses, after, strict=True)}


def test_a_complaint_opens_a_review_with_the_updated_mule_prior(env: Env) -> None:  # noqa: F811
    account = _account(env)
    _credit(env, account, 2_500_000, days_ago=2)
    _credit(env, account, 9_900_000, days_ago=9)  # outside the 7 days
    _complaint(env, account)
    [case] = _sweep(env)
    assert case.type is CaseType.ACCOUNT_REVIEW and case.protocol is CaseProtocol.STANDARD
    assert case.status is CaseStatus.OPEN  # the trigger's evidence is no investigator action
    assert case.subject_account_id == account and case.subject_payment_id is None
    assert case.amount_at_stake_paise == 2_500_000  # docs/02: the 7-day credits
    assert case.sla_due_at == T0 + timedelta(minutes=14_400)  # account_review_sla_minutes
    assert case.prior_components["p_model"] == {"MULE": FALLBACK, "GENUINE": 1 - FALLBACK}
    assert case.prior_components["p_mule_source"] == "fallback"
    expected = _expected_posterior(FALLBACK)
    assert case.posterior == pytest.approx(expected, abs=1e-9)
    assert case.posterior["MULE"] > FALLBACK  # the complaint moved it toward MULE
    assert case.leading_hypothesis == max(expected, key=lambda h: expected[h])
    with env.sessions() as s:
        [record] = current_records(s, case.id)
    assert record.source_key == "LINKED_COMPLAINTS" and record.outcome == "ONE_OR_MORE"
    assert record.posterior_after == pytest.approx(expected, abs=1e-9)
    with env.owner.connect() as conn:
        opened = select(func.count()).where(audit_log.c.action == "case.opened")
        assert conn.execute(opened).scalar_one() == 1
        events = select(func.count()).where(outbox_events.c.type == "case.opened")
        assert conn.execute(events).scalar_one() == 1


def test_the_stored_mule_score_is_used_when_there_is_one(env: Env) -> None:  # noqa: F811
    account = _account(env)
    artifact = uuid4()
    with env.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delphi.model_artifacts (id, kind, version, status, gate_passed,"
                " trained_at, training_cutoff, metrics, feature_list, blob, sha256)"
                " VALUES (:m, 'MULE', 1, 'ACTIVE', true, :t, :t, '{}', '[]', '\\x00',"
                " sha256('\\x00'::bytea))"
            ),
            {"m": artifact, "t": T0},
        )
        conn.execute(
            text(
                "INSERT INTO bank.account_scores (account_id, p_mule, computed_at,"
                " model_artifact_id) VALUES (:a, 0.8, :t, :m)"
            ),
            {"a": account, "t": T0, "m": artifact},
        )
    _complaint(env, account)
    [case] = _sweep(env)
    assert case.prior_components["p_mule_source"] == "account_scores"
    assert case.posterior == pytest.approx(_expected_posterior(0.8), abs=1e-9)


def test_the_trigger_is_idempotent_per_account(env: Env) -> None:  # noqa: F811
    account, other_bank = _account(env), _account(env, bank_code="XBK1")
    _complaint(env, account, other_bank)
    _complaint(env, account, minutes_ago=5)  # a second complaint naming the same account
    assert len(_sweep(env)) == 1  # one review; the external account gets none
    assert _sweep(env) == []  # re-running opens nothing more
    assert _reviews(env) == 1


def test_a_disposed_review_reopens_only_for_a_newer_complaint(env: Env) -> None:  # noqa: F811
    account = _account(env)
    _complaint(env, account)
    [case] = _sweep(env)
    with env.owner.begin() as conn:
        conn.execute(
            text(
                "UPDATE delphi.cases SET status = 'DISPOSED', disposition = 'CLEAR',"
                " disposed_at = :t WHERE id = :c"
            ),
            {"t": T0, "c": case.id},
        )
    env.clock.advance(timedelta(minutes=30))
    assert _sweep(env) == []  # the complaint that opened it does not reopen it
    _complaint(env, account, minutes_ago=1)
    assert len(_sweep(env)) == 1
    assert _reviews(env) == 2


def test_a_complaint_not_yet_reported_opens_nothing(env: Env) -> None:  # noqa: F811
    account = _account(env)
    _complaint(env, account, minutes_ago=-5)  # reported in five minutes
    assert _sweep(env) == []


def test_the_sweep_job_uses_the_active_kb(env: Env) -> None:  # noqa: F811
    account = _account(env)
    _complaint(env, account)
    job = ComplaintSweepJob(env.sessions, env.clock, p_mule_fallback=FALLBACK, demo_time=False)
    ctx: Any = JobContext.__new__(JobContext)
    assert job(ctx) == {"opened": 1}
    assert job(ctx) == {"opened": 0}
    assert _reviews(env) == 1
