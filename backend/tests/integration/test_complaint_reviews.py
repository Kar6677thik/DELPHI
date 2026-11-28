"""The complaints trigger on a real database (docs/09 §1, FR-NAV-01, #109): a complaint naming a
home-bank account opens one ACCOUNT_REVIEW case with the mule-scorer prior updated with
LINKED_COMPLAINTS = ONE_OR_MORE, idempotent per account."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from delphi.engine.belief import update as bayes_update
from delphi.engine.prior import build_prior
from delphi.kb.loader import load_and_validate
from delphi.navigator import reviews
from delphi.navigator.api import CaseProtocol, CaseStatus, CaseType, case_profile, current_records
from delphi.navigator.reviews import (
    AccountReviewSweepJob,
    Backoff,
    SweepLimits,
    open_complaint_reviews,
    open_mule_reviews,
)
from delphi.navigator.tables import cases, review_sweep_failures
from delphi.platform.locks import ACCOUNT_REVIEW_SWEEP_LOCK_KEY
from delphi.platform.tables import audit_log, outbox_events
from delphi.probes.registry import CLASSIFIER_NAMES

from .test_guardian_store import KB_DIR, T0, Env, env  # noqa: F401 - pytest fixture

FALLBACK = 0.10
LOOKBACK = timedelta(days=30)
BACKOFF = Backoff(base=timedelta(seconds=60), cap=timedelta(seconds=3600))


class _Ctx:
    """A job context whose progress reports always succeed."""

    def __init__(self) -> None:
        self.reports: list[float] = []

    def report(self, fraction: float) -> bool:
        self.reports.append(fraction)
        return True


def _job(e: Env, *, batch: int = 25, max_per_run: int = 100) -> AccountReviewSweepJob:
    return AccountReviewSweepJob(
        e.sessions,
        e.clock,
        p_mule_fallback=FALLBACK,
        demo_time=False,
        limits=SweepLimits(
            batch=batch, max_per_run=max_per_run, complaint_lookback=LOOKBACK, backoff=BACKOFF
        ),
    )


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


def _complaint(e: Env, *accounts: UUID, minutes_ago: float = 10) -> None:
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
            s,
            e.clock,
            kb_version_id=e.kb_version_id,
            p_mule_fallback=FALLBACK,
            since=e.clock.now() - LOOKBACK,
            limit=100,
            backoff=BACKOFF,
        ).opened


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


def _score(e: Env, account: UUID, p_mule: float) -> None:
    """A stored mule score, as MULE_SWEEP writes it (#91)."""
    with e.owner.begin() as conn:
        artifact = conn.execute(
            text("SELECT id FROM delphi.model_artifacts WHERE kind = 'MULE'")
        ).scalar_one_or_none()
        if artifact is None:
            artifact = uuid4()
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
                " model_artifact_id) VALUES (:a, :p, :t, :m)"
            ),
            {"a": account, "p": p_mule, "t": T0, "m": artifact},
        )


def test_the_stored_mule_score_is_used_when_there_is_one(env: Env) -> None:  # noqa: F811
    account = _account(env)
    _score(env, account, 0.8)
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
    job = _job(env)
    assert job(cast(Any, _Ctx())) == {
        "opened": 1,
        "complaints": 1,
        "mule_scores": 0,
        "failed": 0,
        "more": False,
    }
    assert job(cast(Any, _Ctx()))["opened"] == 0
    assert _reviews(env) == 1


def test_a_backlog_larger_than_a_batch_clears_over_several_runs(env: Env) -> None:  # noqa: F811
    accounts = [_account(env) for _ in range(7)]
    for n, account in enumerate(accounts):
        _complaint(env, account, minutes_ago=60 - n)
    job = _job(env, batch=2, max_per_run=4)
    ctx = _Ctx()
    assert job(cast(Any, ctx)) == {
        "opened": 4,
        "complaints": 4,
        "mule_scores": 0,
        "failed": 0,
        "more": True,
    }
    assert ctx.reports == [0.5, 1.0]  # one report per committed batch
    with env.owner.connect() as conn:
        first = set(
            conn.execute(
                select(cases.c.subject_account_id).where(
                    cases.c.type == CaseType.ACCOUNT_REVIEW.value
                )
            ).scalars()
        )
    assert first == set(accounts[:4])  # the oldest complaints first
    assert job(cast(Any, _Ctx()))["opened"] == 3
    assert job(cast(Any, _Ctx())) == {
        "opened": 0,
        "complaints": 0,
        "mule_scores": 0,
        "failed": 0,
        "more": False,
    }
    assert _reviews(env) == 7


def test_a_complaint_older_than_the_lookback_opens_nothing(env: Env) -> None:  # noqa: F811
    old, recent = _account(env), _account(env)
    _complaint(env, old, minutes_ago=LOOKBACK.total_seconds() / 60 + 1)
    _complaint(env, recent)
    [case] = _sweep(env)
    assert case.subject_account_id == recent


def test_a_run_skips_while_another_one_sweeps(env: Env) -> None:  # noqa: F811
    _complaint(env, _account(env))
    with env.owner.begin() as conn:
        conn.execute(select(func.pg_advisory_xact_lock(ACCOUNT_REVIEW_SWEEP_LOCK_KEY)))
        assert _job(env)(cast(Any, _Ctx()))["skipped"] == "busy"
    assert _reviews(env) == 0


def _mule_sweep(e: Env, limit: int = 100) -> list[Any]:
    with e.sessions.begin() as s:
        return open_mule_reviews(
            s, e.clock, kb_version_id=e.kb_version_id, limit=limit, backoff=BACKOFF
        ).opened


def test_a_credited_suspect_gets_a_review_with_the_mule_prior(env: Env) -> None:  # noqa: F811
    """docs/09 §1: p_mule ≥ 0.70 and a credit in the last 24 h; the prior alone."""
    suspect, quiet, low = _account(env), _account(env), _account(env)
    external = _account(env, bank_code="XBK1")
    for account, p in ((suspect, 0.9), (quiet, 0.95), (low, 0.6), (external, 0.99)):
        _score(env, account, p)
    for account in (suspect, low, external):
        _credit(env, account, 1_000_000, days_ago=0.5)
    _credit(env, quiet, 1_000_000, days_ago=2)  # credited, but not in the last 24 h
    [case] = _mule_sweep(env)
    assert case.subject_account_id == suspect and case.status is CaseStatus.OPEN
    assert case.prior_components["p_model"] == {"MULE": 0.9, "GENUINE": pytest.approx(0.1)}
    kb = load_and_validate(KB_DIR, CLASSIFIER_NAMES)
    profile = case_profile(kb, CaseType.ACCOUNT_REVIEW.value)
    prior = build_prior(profile, {"MULE": 0.9, "GENUINE": 0.1}, 0.0)
    assert case.posterior == pytest.approx(
        dict(zip(profile.hypotheses, map(float, prior), strict=True)), abs=1e-9
    )
    with env.sessions() as s:
        assert current_records(s, case.id) == []  # no complaint to add
    assert _mule_sweep(env) == []  # idempotent


def test_only_new_money_reopens_a_disposed_suspect(env: Env) -> None:  # noqa: F811
    suspect = _account(env)
    _score(env, suspect, 0.9)
    _credit(env, suspect, 1_000_000, days_ago=0.5)
    _credit(env, suspect, 2_000_000, days_ago=0.4)  # two credits: still one review
    [case] = _mule_sweep(env)
    with env.owner.begin() as conn:
        conn.execute(
            text(
                "UPDATE delphi.cases SET status = 'DISPOSED', disposition = 'CLEAR',"
                " disposed_at = :t WHERE id = :c"
            ),
            {"t": T0, "c": case.id},
        )
    env.clock.advance(timedelta(minutes=10))
    assert _mule_sweep(env) == []
    _credit(env, suspect, 500_000, days_ago=0)  # new money after the review
    assert len(_mule_sweep(env)) == 1


def test_one_busy_mule_does_not_fill_a_batch(env: Env) -> None:  # noqa: F811
    busy, other = _account(env), _account(env)
    for account in (busy, other):
        _score(env, account, 0.9)
    for minutes in range(5):  # the five oldest credits are all the busy mule's
        _credit(env, busy, 100_000, days_ago=0.5 - minutes / 1440)
    _credit(env, other, 100_000, days_ago=0.1)
    opened = _mule_sweep(env, limit=2)
    assert {c.subject_account_id for c in opened} == {busy, other}


def test_both_triggers_share_one_cap_per_run(env: Env) -> None:  # noqa: F811
    complained = [_account(env) for _ in range(2)]
    suspects = [_account(env) for _ in range(3)]
    for account in complained:
        _complaint(env, account)
    for account in suspects:
        _score(env, account, 0.9)
        _credit(env, account, 1_000_000, days_ago=0.1)
    job = _job(env, batch=2, max_per_run=3)
    assert job(cast(Any, _Ctx())) == {
        "opened": 3,
        "complaints": 2,  # the complaints first
        "mule_scores": 1,
        "failed": 0,
        "more": True,
    }
    assert job(cast(Any, _Ctx()))["mule_scores"] == 2
    assert _reviews(env) == 5


def _fail_for(monkeypatch: pytest.MonkeyPatch, bad: set[UUID]) -> None:
    """Make opening the review of any account in ``bad`` fail, as a broken evidence fetch or
    case insert would (#290); the others open as usual."""
    real = reviews.open_account_review

    def flaky(session: Any, clock: Any, *, account_id: UUID, **kw: Any) -> Any:
        if account_id in bad:
            raise RuntimeError(f"cannot open {account_id}")
        return real(session, clock, account_id=account_id, **kw)

    monkeypatch.setattr(reviews, "open_account_review", flaky)


def test_a_candidate_lost_to_a_concurrent_open_counts_against_the_cap(
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#306 CodeRabbit finding: another caller opening the review first opens nothing and fails
    nothing here, but it's still an attempt, so a one-candidate cap tries no second one."""
    raced = _account(env)
    _complaint(env, raced)
    suspect = _account(env)
    _score(env, suspect, 0.9)
    _credit(env, suspect, 1_000_000, days_ago=0.1)

    calls: list[UUID] = []

    def lost(*_args: Any, account_id: UUID, **_kw: Any) -> Any:
        calls.append(account_id)
        raise reviews.CaseExistsError("opened by someone else")

    monkeypatch.setattr(reviews, "open_account_review", lost)
    job = _job(env, batch=1, max_per_run=1)
    assert job(cast(Any, _Ctx())) == {
        "opened": 0,
        "complaints": 0,
        "mule_scores": 0,
        "failed": 0,
        "more": True,
    }
    assert calls == [raced]  # the suspect was not tried: the cap of one was used
    assert _failure(env, raced) is None and _failure(env, suspect) is None


def _failure(e: Env, account: UUID) -> Any:
    with e.owner.connect() as conn:
        return conn.execute(
            select(review_sweep_failures).where(review_sweep_failures.c.account_id == account)
        ).one_or_none()


def _subjects(e: Env) -> set[UUID]:
    with e.owner.connect() as conn:
        return set(
            conn.execute(
                select(cases.c.subject_account_id).where(
                    cases.c.type == CaseType.ACCOUNT_REVIEW.value
                )
            ).scalars()
        )


@pytest.mark.parametrize("batch", [1, 3])
def test_a_failing_candidate_backs_off_and_the_others_still_open(
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    batch: int,
) -> None:
    """#290: the oldest candidate always fails. With batch 1 the others open in later batches,
    with batch 3 in the same one; the failure is recorded and backed off, then retried."""
    bad, *good = [_account(env) for _ in range(3)]
    for n, account in enumerate([bad, *good]):
        _complaint(env, account, minutes_ago=60 - n)
    _fail_for(monkeypatch, {bad})
    job = _job(env, batch=batch)
    assert job(cast(Any, _Ctx())) == {
        "opened": 2,
        "complaints": 2,
        "mule_scores": 0,
        "failed": 1,
        "more": False,
    }
    assert _subjects(env) == set(good)
    failure = _failure(env, bad)
    assert failure.attempts == 1 and failure.last_error.startswith("RuntimeError: cannot open")
    assert failure.next_attempt_at == env.clock.now() + timedelta(seconds=60)

    # Backing off: not tried again before its next attempt.
    env.clock.advance(timedelta(seconds=59))
    assert job(cast(Any, _Ctx()))["failed"] == 0
    # Retried, fails again: the wait doubles.
    env.clock.advance(timedelta(seconds=1))
    assert job(cast(Any, _Ctx()))["failed"] == 1
    failure = _failure(env, bad)
    assert failure.attempts == 2
    assert failure.next_attempt_at == env.clock.now() + timedelta(seconds=120)

    # Fixed: the next attempt opens it and clears the record.
    monkeypatch.undo()
    env.clock.advance(timedelta(seconds=120))
    assert job(cast(Any, _Ctx()))["complaints"] == 1
    assert _subjects(env) == {bad, *good}
    assert _failure(env, bad) is None


def test_a_failing_suspect_backs_off_too(
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#290, the mule-scorer trigger: the failing suspect backs off, the other opens."""
    bad, good = _account(env), _account(env)
    for n, account in enumerate((bad, good)):
        _score(env, account, 0.9)
        _credit(env, account, 1_000_000, days_ago=0.5 - n / 1440)
    _fail_for(monkeypatch, {bad})
    with env.sessions.begin() as s:
        swept = open_mule_reviews(
            s, env.clock, kb_version_id=env.kb_version_id, limit=10, backoff=BACKOFF
        )
    assert [c.subject_account_id for c in swept.opened] == [good]
    assert swept.failed == [bad]
    assert _failure(env, bad).attempts == 1
    assert _mule_sweep(env) == []  # backing off
