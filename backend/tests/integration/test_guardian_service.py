"""The Guardian session service on a real database (docs/08 §2–3, docs/05 §2–3, #94 part 2):
the prior, the fast path, the passive phase, planning, the decision with the payment's status
and the case, and the ENGINE_ERROR fail-safe. Runs as `delphi_app`."""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from delphi.guardian import procedure as procedure_module
from delphi.guardian import service as service_module
from delphi.guardian.service import Decided, GuardianService, NextProbe, PriorInputs
from delphi.guardian.store import (
    ProbeMode,
    ProbeRunStatus,
    Runner,
    SessionExistsError,
    SessionMode,
    SessionStatus,
    TerminalReason,
    list_probe_runs,
)
from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.kb.loader import load_and_validate
from delphi.kb.store import ensure_kb_v1
from delphi.navigator.api import get_case
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.errors import DelphiError
from delphi.probes.api import passive_executor
from delphi.probes.classifiers import (
    BeneficiaryIntelObs,
    CallStatusObs,
    DevicePostureObs,
    PhoneRiskObs,
    SessionBehaviourObs,
)
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sim import api as bank

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
KB_DIR = Path(__file__).resolve().parents[3] / "kb"
INR = 100  # paise
OTP_KEY = b"test-otp-key-32-bytes-long-enough"

GENUINE_SURE = {
    "GENUINE": 0.999,
    "ATO": 0.0004,
    "APP_DIGITAL_ARREST": 0.0003,
    "APP_INVESTMENT": 0.0003,
}
GENUINE = {"GENUINE": 0.97, "ATO": 0.01, "APP_DIGITAL_ARREST": 0.01, "APP_INVESTMENT": 0.01}
DIGITAL_ARREST = {"GENUINE": 0.2, "ATO": 0.02, "APP_DIGITAL_ARREST": 0.75, "APP_INVESTMENT": 0.03}

CLEAN = {
    "BENEFICIARY_INTEL": "CLEAN_KNOWN_ENTITY",
    "COUNTERPARTY_PHONE_RISK": "LOW_OR_UNKNOWN",
    "DEVICE_POSTURE": "KNOWN_DEVICE",
    "SESSION_BEHAVIOUR": "NORMAL",
    "CALL_STATUS": "NOT_ON_CALL",
}
SCAM = {
    "BENEFICIARY_INTEL": "LISTED_SUSPECT",
    "COUNTERPARTY_PHONE_RISK": "VERY_HIGH",
    "DEVICE_POSTURE": "KNOWN_DEVICE",
    "SESSION_BEHAVIOUR": "HESITANT_OR_PASTED",
    "CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER",
}
UNSURE = {**CLEAN, "BENEFICIARY_INTEL": "CLEAN_UNKNOWN", "CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER"}


def _observation(key: str, outcome: str, as_of: datetime) -> object:
    """A raw observation that the real classifier turns into ``outcome``."""
    match key, outcome:
        case "BENEFICIARY_INTEL", "CLEAN_KNOWN_ENTITY":
            return BeneficiaryIntelObs(False, 0.0, True, 0, 0, None, as_of)
        case "BENEFICIARY_INTEL", "CLEAN_UNKNOWN":
            return BeneficiaryIntelObs(False, 0.0, False, 0, 0, None, as_of)
        case "BENEFICIARY_INTEL", "LISTED_SUSPECT":
            return BeneficiaryIntelObs(True, 0.0, False, 0, 0, None, as_of)
        case "COUNTERPARTY_PHONE_RISK", _:
            return PhoneRiskObs(None if outcome == "LOW_OR_UNKNOWN" else outcome)
        case "DEVICE_POSTURE", "KNOWN_DEVICE":
            return DevicePostureObs(False, False, as_of - timedelta(days=400), as_of)
        case "DEVICE_POSTURE", "NEW_DEVICE":
            return DevicePostureObs(False, False, as_of - timedelta(days=1), as_of)
        case "SESSION_BEHAVIOUR", "NORMAL":
            return SessionBehaviourObs(False, 2.0, 0.0)
        case "SESSION_BEHAVIOUR", "HESITANT_OR_PASTED":
            return SessionBehaviourObs(True, 2.0, 0.0)
        case "CALL_STATUS", "NOT_ON_CALL":
            return CallStatusObs(False, None)
        case "CALL_STATUS", "ON_CALL_UNKNOWN_NUMBER":
            return CallStatusObs(True, False)
    raise AssertionError(f"no fixture observation for {key} → {outcome}")


def fetchers(outcomes: Mapping[str, str], slow: str | None = None) -> dict[str, Any]:
    release = threading.Event()

    def make(key: str, outcome: str) -> Callable[[Any], object]:
        def fetch(request: Any) -> object:
            if key == slow:
                release.wait(5)  # far beyond the KB's 300 ms adapter timeout
            return _observation(key, outcome, request.as_of)

        return fetch

    built = {k: make(k, o) for k, o in outcomes.items()}
    built["_release"] = release.set  # type: ignore[assignment]
    return built


@dataclass
class FixedPrior:
    p_payment: Mapping[str, float]
    q: float = 0.5
    p_mule: float | None = 0.01
    raises: bool = False
    calls: list[UUID] = field(default_factory=list)

    def prior_inputs(self, payment: bank.PaymentView) -> PriorInputs:
        self.calls.append(payment.id)
        if self.raises:
            raise RuntimeError("sensing is down")
        top = ({"feature": "amount_log10", "value": 3.0, "contribution": 0.4},)
        return PriorInputs(self.p_payment, self.q, self.p_mule, top)


@dataclass
class Env:
    owner: Engine
    sessions: sessionmaker[Session]
    clock: FixedClock

    def payment(
        self, amount_inr: int, *, balance_inr: int = 1_000_000, payment_id: UUID | None = None
    ) -> UUID:
        customer, payer, payee = uuid4(), uuid4(), uuid4()
        payment = payment_id or uuid4()
        with self.owner.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO bank.customers (id, display_name, age, language, is_vulnerable,"
                    " segment, income_band, consent_call_status, home_city)"
                    " VALUES (:c, 'CUST', 68, 'te', false, 'RETAIL', 'MID', true, 'Vijayawada')"
                ),
                {"c": customer},
            )
            for account, owner, balance in ((payer, customer, balance_inr), (payee, None, 0)):
                conn.execute(
                    text(
                        "INSERT INTO bank.accounts (id, customer_id, bank_code, opened_at,"
                        " kyc_level, status, balance_paise)"
                        " VALUES (:a, :c, 'DLPH', :t, 'FULL', 'ACTIVE', :b)"
                    ),
                    {"a": account, "c": owner, "t": T0 - timedelta(days=900), "b": balance * INR},
                )
            conn.execute(
                text(
                    "INSERT INTO bank.payments (id, payer_account_id, payee_account_id,"
                    " amount_paise, channel, initiated_at, status, origin)"
                    " VALUES (:p, :a, :b, :m, 'UPI_PUSH', :t, 'PENDING_GUARDIAN', 'DEMO')"
                ),
                {"p": payment, "a": payer, "b": payee, "m": amount_inr * INR, "t": T0},
            )
        return payment

    def another_payment(self, like: UUID, amount_inr: int) -> UUID:
        """A second pending payment from the same payer to the same payee."""
        payment = uuid4()
        with self.owner.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO bank.payments (id, payer_account_id, payee_account_id,"
                    " amount_paise, channel, initiated_at, status, origin)"
                    " SELECT :p, payer_account_id, payee_account_id, :m, channel, :t,"
                    " 'PENDING_GUARDIAN', origin FROM bank.payments WHERE id = :like"
                ),
                {"p": payment, "m": amount_inr * INR, "t": self.clock.now(), "like": like},
            )
        return payment

    def service(self, prior: FixedPrior, outcomes: Mapping[str, str], **kw: Any) -> GuardianService:
        fetch = fetchers(outcomes, kw.pop("slow", None))
        self._release = fetch.pop("_release")
        return GuardianService(
            self.sessions,
            self.clock,
            prior,
            fetch,
            passive_executor(threads=8),
            otp_key=OTP_KEY,
            **kw,
        )

    def release_slow(self) -> None:
        self._release()

    def payment_row(self, payment_id: UUID) -> Any:
        with self.owner.connect() as conn:
            return conn.execute(
                select(bank.payments.c.status, bank.payments.c.executed_at).where(
                    bank.payments.c.id == payment_id
                )
            ).one()

    def count(self, sql: str, **params: Any) -> int:
        with self.owner.connect() as conn:
            return int(conn.execute(text(sql), params).scalar_one())


def make_env(create_database: Callable[[], str]) -> Iterator[Env]:
    """A migrated database with KB v1 ACTIVE, the bank written as the owner, the service run
    as the api's role. Shared with test_guardian_active."""
    url = create_database()
    upgrade(url)
    owner = engine_as(url)
    app = engine_as(url, "delphi_app")
    clock = FixedClock(T0)
    with make_session_factory(owner).begin() as s:
        assert ensure_kb_v1(s, clock, load_and_validate(KB_DIR, CLASSIFIER_NAMES)) is not None
    yield Env(owner, make_session_factory(app), clock)
    app.dispose()
    owner.dispose()


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    yield from make_env(create_database)


def _run(env: Env, svc: GuardianService, payment_id: UUID) -> Decided | NextProbe:
    """Steps 1–6 as the runner (#95) will drive them."""
    started = svc.start(payment_id, mode=SessionMode.LIVE, runner=Runner.API)
    if started.status is SessionStatus.DECIDED:
        return Decided(started)
    asyncio.run(svc.passive(started.id))
    return svc.plan(started.id)


# ---------------------------------------------------------------- start and fast path


def test_start_pins_the_kb_and_persists_the_prior_and_its_parts(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE, q=0.95, p_mule=0.02), CLEAN)
    record = svc.start(env.payment(480_000), mode=SessionMode.LIVE, runner=Runner.API)
    assert record.status is SessionStatus.CREATED  # not the fast path: ₹4,80,000
    with env.owner.connect() as conn:
        row = conn.execute(
            select(guardian_sessions).where(guardian_sessions.c.id == record.id)
        ).one()
    assert row.p_model["MULE"] == pytest.approx(0.02)  # the payer's mule score (docs/06 §11)
    assert row.p_model["GENUINE"] == pytest.approx(0.97 * 0.98)
    assert row.anomaly_percentile == 0.95
    assert row.open_mass == pytest.approx(0.02 + 0.18 * 0.5)  # q above the 90th percentile
    assert sum(row.prior.values()) == pytest.approx(1.0)
    assert row.posterior == row.prior
    assert set(row.risk_by_decision) == {"RELEASE", "HOLD_FOR_REVIEW", "PROTECTIVE_HOLD", "DECLINE"}
    assert row.top_features[0]["feature"] == "amount_log10"
    assert (
        env.count("SELECT count(*) FROM delphi.audit_log WHERE action = 'guardian.session.created'")
        == 1
    )


def test_the_fast_path_releases_and_executes_without_probes(env: Env) -> None:
    payment = env.payment(2_000)
    result = _run(env, env.service(FixedPrior(GENUINE_SURE, p_mule=None), CLEAN), payment)
    assert isinstance(result, Decided)
    session = result.session
    assert (session.decision, session.terminal_reason) == ("RELEASE", TerminalReason.FAST_PATH)
    assert env.payment_row(payment).status == "EXECUTED"
    assert (
        env.count("SELECT count(*) FROM bank.ledger_entries WHERE payment_id = :p", p=payment) == 2
    )
    assert env.count("SELECT count(*) FROM delphi.probe_runs") == 0  # docs/08 §3: no probes


def test_a_payment_is_started_once_and_only_while_pending(env: Env) -> None:
    payment = env.payment(480_000)
    svc = env.service(FixedPrior(GENUINE), CLEAN)
    svc.start(payment, mode=SessionMode.LIVE, runner=Runner.API)
    with pytest.raises(SessionExistsError):
        svc.start(payment, mode=SessionMode.LIVE, runner=Runner.API)
    done = env.payment(2_000)
    _run(env, env.service(FixedPrior(GENUINE_SURE, p_mule=None), CLEAN), done)
    with pytest.raises(DelphiError, match="not waiting for the Guardian"):
        svc.start(done, mode=SessionMode.LIVE, runner=Runner.API)


# ---------------------------------------------------------------- passive phase


def test_passive_probes_update_in_kb_order_and_a_slow_adapter_does_not_block(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE), UNSURE, slow="COUNTERPARTY_PHONE_RISK")
    started = svc.start(env.payment(480_000), mode=SessionMode.LIVE, runner=Runner.API)
    try:
        session = asyncio.run(svc.passive(started.id))
    finally:
        env.release_slow()
    assert session.status is SessionStatus.PLANNING
    with env.owner.connect() as conn:
        runs = conn.execute(
            select(probe_runs)
            .where(probe_runs.c.session_id == started.id)
            .order_by(probe_runs.c.seq)
        ).all()
    assert [r.probe_key for r in runs] == [
        "BENEFICIARY_INTEL",
        "COUNTERPARTY_PHONE_RISK",
        "DEVICE_POSTURE",
        "SESSION_BEHAVIOUR",
        "CALL_STATUS",
    ]
    slow = runs[1]
    assert (slow.status, slow.outcome) == ("ERROR", None)  # FR-GDN-03: no update
    assert slow.raw_response == {"error": "TIMEOUT"}
    assert slow.posterior_after == slow.posterior_before
    for before, after in itertools.pairwise(runs):
        assert after.posterior_before == before.posterior_after  # each update chains on
    assert all(r.status == "COMPLETED" for r in runs if r is not slow)
    assert runs[-1].outcome == "ON_CALL_UNKNOWN_NUMBER" and runs[-1].info_yield_bits > 0
    assert session.posterior == runs[-1].posterior_after
    # resuming the step does nothing: the session is no longer CREATED
    assert asyncio.run(svc.passive(started.id)).version == session.version


# ---------------------------------------------------------------- planning and the decision


def test_planning_issues_a_pending_active_probe_with_its_candidates(env: Env) -> None:
    payment = env.payment(50_000)
    result = _run(env, env.service(FixedPrior(GENUINE, q=1.0, p_mule=None), UNSURE), payment)
    assert isinstance(result, NextProbe) and result.probe_key == "PURPOSE_QUESTION"
    with env.sessions() as s:
        active = [r for r in list_probe_runs(s, result.session.id) if r.mode is ProbeMode.ACTIVE]
    assert [(r.probe_key, r.status) for r in active] == [
        ("PURPOSE_QUESTION", ProbeRunStatus.PENDING)
    ]
    with env.owner.connect() as conn:
        run = conn.execute(select(probe_runs).where(probe_runs.c.id == result.probe_run_id)).one()
    assert run.selected_by in {"GREEDY", "EXPLORE"} and 0 < run.propensity <= 1
    assert run.candidates[0]["source_key"] == "PURPOSE_QUESTION" and run.candidates[0]["selected"]
    assert "PURPOSE_QUESTION" in run.eligible_set and run.lookahead_depth >= 1
    assert run.posterior_before == result.session.posterior
    assert env.payment_row(payment).status == "PENDING_GUARDIAN"  # nothing decided yet


def test_a_clean_large_payment_is_released_on_stop(env: Env) -> None:
    payment = env.payment(480_000)
    result = _run(env, env.service(FixedPrior(GENUINE), CLEAN), payment)
    assert isinstance(result, Decided)
    assert (result.session.decision, result.session.terminal_reason) == (
        "RELEASE",
        TerminalReason.STOP,
    )
    assert env.payment_row(payment).status == "EXECUTED"
    assert result.session.case_id is None and not result.session.unexplained


def test_a_release_the_payer_cannot_fund_stays_released(env: Env) -> None:
    payment = env.payment(480_000, balance_inr=1_000)
    result = _run(env, env.service(FixedPrior(GENUINE), CLEAN), payment)
    assert isinstance(result, Decided) and result.session.decision == "RELEASE"
    row = env.payment_row(payment)
    assert (row.status, row.executed_at) == ("RELEASED", None)  # docs/05 §3, #94
    assert (
        env.count("SELECT count(*) FROM bank.ledger_entries WHERE payment_id = :p", p=payment) == 0
    )


def test_a_protective_hold_holds_the_payment_and_opens_a_coercion_case(env: Env) -> None:
    payment = env.payment(480_000)
    result = _run(env, env.service(FixedPrior(DIGITAL_ARREST), SCAM), payment)
    assert isinstance(result, Decided)
    session = result.session
    assert (session.decision, session.terminal_reason) == ("PROTECTIVE_HOLD", TerminalReason.STOP)
    assert env.payment_row(payment).status == "HELD"
    assert session.case_id is not None
    with env.sessions() as s:
        case = get_case(s, session.case_id)
    assert case is not None and case.protocol.value == "COERCION"
    assert case.posterior == session.posterior and case.guardian_session_id == session.id
    assert (
        env.count("SELECT count(*) FROM delphi.audit_log WHERE action = 'guardian.session.decided'")
        == 1
    )
    assert (
        env.count(
            "SELECT count(*) FROM delphi.outbox_events WHERE type = 'guardian.session.decided'"
        )
        == 1
    )


def test_a_session_past_its_maximum_duration_decides_on_its_posterior(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE, q=1.0, p_mule=None), UNSURE)
    started = svc.start(env.payment(50_000), mode=SessionMode.LIVE, runner=Runner.API)
    asyncio.run(svc.passive(started.id))
    env.clock.advance(timedelta(hours=1))  # beyond kb guardrails.session_max_duration_s
    result = svc.plan(started.id)
    assert isinstance(result, Decided)
    assert result.session.terminal_reason is TerminalReason.SESSION_TIMEOUT


# ---------------------------------------------------------------- the fail-safe (FR-GDN-13)


def test_a_sensing_failure_holds_for_review_with_engine_error(env: Env) -> None:
    payment = env.payment(480_000)
    record = env.service(FixedPrior(GENUINE, raises=True), CLEAN).start(
        payment, mode=SessionMode.LIVE, runner=Runner.API
    )
    assert record.status is SessionStatus.DECIDED
    assert (record.decision, record.terminal_reason) == (
        "HOLD_FOR_REVIEW",
        TerminalReason.ENGINE_ERROR,
    )
    assert env.payment_row(payment).status == "HELD"
    assert record.case_id is not None
    with env.sessions() as s:
        case = get_case(s, record.case_id)
    assert case is not None and case.protocol.value == "STANDARD"


def test_an_engine_error_while_planning_rolls_back_and_holds(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = env.service(FixedPrior(GENUINE, q=1.0, p_mule=None), UNSURE)
    started = svc.start(env.payment(50_000), mode=SessionMode.LIVE, runner=Runner.API)
    asyncio.run(svc.passive(started.id))

    def broken(*_args: object, **_kw: object) -> None:
        raise FloatingPointError("injected")

    # The planner runs in the pure procedure the service calls (#123).
    monkeypatch.setattr(procedure_module, "plan_next", broken)
    result = svc.plan(started.id)
    assert isinstance(result, Decided)
    assert result.session.terminal_reason is TerminalReason.ENGINE_ERROR
    assert result.session.decision == "HOLD_FOR_REVIEW"
    with env.owner.connect() as conn:  # the failed step left no active probe run behind
        active = conn.execute(
            select(func.count())
            .select_from(probe_runs)
            .where(probe_runs.c.session_id == started.id, probe_runs.c.mode == "ACTIVE")
        ).scalar_one()
    assert active == 0
    # a finished session is left alone, and planning it again is the caller's mistake
    assert svc.fail_safe(started.id, RuntimeError("again")).version == result.session.version
    with pytest.raises(DelphiError, match="not PLANNING"):
        svc.plan(started.id)


def test_the_bank_refuses_bad_decisions_unknown_and_finished_payments(env: Env) -> None:
    payment = env.payment(1_000)
    with env.sessions.begin() as s:
        with pytest.raises(DelphiError, match="Unknown decision"):
            bank.apply_guardian_decision(s, payment, "SHRUG", T0)
        with pytest.raises(DelphiError, match="No payment"):
            bank.apply_guardian_decision(s, uuid4(), "RELEASE", T0)
        assert (
            bank.apply_guardian_decision(s, payment, "DECLINE", T0) is bank.PaymentStatus.DECLINED
        )
        with pytest.raises(DelphiError, match="not waiting for the Guardian"):
            bank.apply_guardian_decision(s, payment, "RELEASE", T0)


# ---------------------------------------------------------------- review fixes (#169)


def test_planning_again_returns_the_pending_probe_instead_of_adding_one(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE, q=1.0, p_mule=None), UNSURE)
    first = _run(env, svc, env.payment(50_000))
    assert isinstance(first, NextProbe) and first.plan is not None
    again = svc.plan(first.session.id)  # a resume after a crash between plan and issue
    assert isinstance(again, NextProbe) and again.plan is None
    assert (again.probe_run_id, again.probe_key) == (first.probe_run_id, first.probe_key)
    with env.sessions() as s:
        active = [r for r in list_probe_runs(s, first.session.id) if r.mode is ProbeMode.ACTIVE]
    assert len(active) == 1


def test_a_session_left_in_passive_probing_by_a_crash_is_resumed(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE), CLEAN)
    started = svc.start(env.payment(480_000), mode=SessionMode.LIVE, runner=Runner.API)
    with env.owner.begin() as conn:  # the crash: moved to PASSIVE_PROBING, nothing applied
        conn.execute(
            guardian_sessions.update()
            .where(guardian_sessions.c.id == started.id)
            .values(status="PASSIVE_PROBING", version=guardian_sessions.c.version + 1)
        )
    resumed = asyncio.run(svc.passive(started.id))
    assert resumed.status is SessionStatus.PLANNING
    assert (
        env.count("SELECT count(*) FROM delphi.probe_runs WHERE session_id = :s", s=started.id) == 5
    )


def test_a_decision_closes_the_open_probe_run(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE, q=1.0, p_mule=None), UNSURE)
    step = _run(env, svc, env.payment(50_000))
    assert isinstance(step, NextProbe)
    decided = svc.fail_safe(step.session.id, RuntimeError("injected"))
    assert decided.terminal_reason is TerminalReason.ENGINE_ERROR
    with env.owner.connect() as conn:
        run = conn.execute(select(probe_runs).where(probe_runs.c.id == step.probe_run_id)).one()
    assert (run.status, run.raw_response) == ("ERROR", {"closed_by": "ENGINE_ERROR"})


def test_when_the_fail_safe_itself_fails_the_session_still_ends(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    payment = env.payment(480_000)
    svc = env.service(FixedPrior(GENUINE, raises=True), CLEAN)

    def bank_down(*_args: object, **_kw: object) -> None:
        raise ConnectionError("bank unavailable")

    monkeypatch.setattr(service_module.bank, "apply_guardian_decision", bank_down)
    record = svc.start(payment, mode=SessionMode.LIVE, runner=Runner.API)
    assert (record.status, record.decision) == (SessionStatus.DECIDED, "HOLD_FOR_REVIEW")
    assert record.terminal_reason is TerminalReason.ENGINE_ERROR and record.case_id is None
    assert env.payment_row(payment).status == "PENDING_GUARDIAN"  # left to an operator
    assert (
        env.count(
            "SELECT count(*) FROM delphi.audit_log"
            " WHERE action = 'guardian.session.fail_safe_failed'"
        )
        == 1
    )


def test_the_b1_and_b2_verdicts_are_stored_with_the_prior(env: Env) -> None:
    """docs/08 §6, FR-GDN-12: what B1 and B2 (docs/22 §2) would have done, for the monitor.
    ₹4,80,000 to a payee never saved, from no known device: B1 holds. B2's r = 1 − P(GENUINE)
    from this same prior: low for a confident genuine prior, high for a digital-arrest one."""
    svc = env.service(FixedPrior(GENUINE_SURE, p_mule=None), CLEAN)
    calm = svc.start(env.payment(480_000), mode=SessionMode.LIVE, runner=Runner.API)
    small = svc.start(env.payment(2_000), mode=SessionMode.LIVE, runner=Runner.API)
    scam = env.service(FixedPrior(DIGITAL_ARREST, p_mule=None), CLEAN).start(
        env.payment(480_000), mode=SessionMode.LIVE, runner=Runner.API
    )
    with env.owner.connect() as conn:
        stored = {
            r.id: r.baseline_decisions
            for r in conn.execute(
                select(guardian_sessions.c.id, guardian_sessions.c.baseline_decisions)
            )
        }
    assert stored[calm.id] == {"B1": "HOLD_FOR_REVIEW", "B2": "RELEASE"}
    assert stored[small.id] == {"B1": "RELEASE", "B2": "RELEASE"}
    assert stored[scam.id] == {"B1": "HOLD_FOR_REVIEW", "B2": "HOLD_FOR_REVIEW"}


def test_b1_counts_only_executed_payments_and_no_device_as_not_new(env: Env) -> None:
    """#186 review: the monitor's B1 inputs match the history's (``history.py`` ``_decide``). A
    held attempt is no earlier payment, and a payment with no device is not from a new device."""
    held = env.payment(60_000)
    with env.owner.begin() as conn:
        conn.execute(
            text("UPDATE bank.payments SET status = 'HELD', initiated_at = :t WHERE id = :p"),
            {"t": T0 - timedelta(days=3), "p": held},
        )
    retry = env.another_payment(held, 60_000)  # the same payer and payee, days later
    small = env.payment(15_000)  # ₹15,000 with no device_id: under B1's new-payee threshold
    svc = env.service(FixedPrior(GENUINE_SURE, p_mule=None), CLEAN)
    retried = svc.start(retry, mode=SessionMode.LIVE, runner=Runner.API)
    deviceless = svc.start(small, mode=SessionMode.LIVE, runner=Runner.API)
    with env.owner.connect() as conn:
        stored = {
            r.id: r.baseline_decisions
            for r in conn.execute(
                select(guardian_sessions.c.id, guardian_sessions.c.baseline_decisions)
            )
        }
    assert stored[retried.id]["B1"] == "HOLD_FOR_REVIEW"  # still a new payee: the attempt was held
    assert stored[deviceless.id]["B1"] == "RELEASE"  # no device is not a new device
