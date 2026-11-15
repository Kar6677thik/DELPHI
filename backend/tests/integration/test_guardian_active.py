"""Active probes through the Guardian service on a real database (docs/08 §2 step 7, §4–5,
#94 part 3): issuing, answers, the OTP protocol, timeouts, cancel, and the ENG-S1/S2 stories
driven end to end through the service with a scripted customer (docs/21 §3)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from delphi.guardian.active import ClientSignals, Ui
from delphi.guardian.service import Decided, GuardianService, IssuedProbe, NextProbe
from delphi.guardian.store import (
    ProbeMode,
    Runner,
    SessionMode,
    SessionStatus,
    TerminalReason,
    add_probe_run,
)
from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.navigator.api import get_case
from delphi.platform.errors import DelphiError, ProblemCode

from .test_guardian_service import CLEAN, GENUINE, UNSURE, Env, FixedPrior, make_env


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    yield from make_env(create_database)


U = 0.02  # open_mass_base: the open mass below the 90th anomaly percentile (q = 0.5)


def story_prior(p0: tuple[float, ...]) -> FixedPrior:
    """The docs/06 §17 fixture prior (G, ATO, DA, INV, MULE, ∅) as sensing inputs that rebuild
    it exactly: MULE from the payer's mule score, ∅ = the open mass (docs/06 §11)."""
    genuine, ato, da, inv, mule, unexplained = p0
    assert unexplained == pytest.approx(U)
    p_mule = mule / (1 - U)
    scale = (1 - U) * (1 - p_mule)
    p_payment = {
        "GENUINE": genuine / scale,
        "ATO": ato / scale,
        "APP_DIGITAL_ARREST": da / scale,
        "APP_INVESTMENT": inv / scale,
    }
    return FixedPrior(p_payment, q=0.5, p_mule=p_mule)


# docs/06 §17 and docs/21 §3 (ENG-S1/S2), as in tests/golden/test_story_behaviour.py
ARJUN_PRIOR = (0.69, 0.20, 0.04, 0.03, 0.02, 0.02)
ARJUN_PASSIVE = {**CLEAN, "DEVICE_POSTURE": "NEW_DEVICE"}
KAMALA_PRIOR = (0.54, 0.08, 0.30, 0.05, 0.01, 0.02)
KAMALA_PASSIVE = {
    **CLEAN,
    "BENEFICIARY_INTEL": "CLEAN_UNKNOWN",
    "CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER",
}

# Raw answers: (answer, latency in seconds, app switches)
GENUINE_ANSWERS: dict[str, tuple[dict[str, Any], float, int]] = {
    "BIOMETRIC_STEP_UP": ({"biometric": "MATCH"}, 3.0, 0),
    "PURPOSE_QUESTION": ({"option_key": "BILLS_OR_SHOPPING"}, 5.0, 0),
    "SPELL_BREAKER_DIGITAL_ARREST": ({"option_key": "NO"}, 3.0, 0),
    "SPELL_BREAKER_INVESTMENT": ({"option_key": "NO"}, 3.0, 0),
    "COLLECT_INTENT_CHECK": ({"option_key": "NO_I_AM_PAYING"}, 3.0, 0),
    "COOLING_OFF": ({"cooling_off": {"cancelled": False}}, 30.0, 0),
}
KAMALA_ANSWERS = {  # S-1, coached: ENG-S1's answers
    **GENUINE_ANSWERS,
    "PURPOSE_QUESTION": ({"option_key": "FAMILY_OR_FRIEND"}, 6.0, 0),
    "SPELL_BREAKER_DIGITAL_ARREST": ({"option_key": "NO"}, 11.0, 1),
    "COOLING_OFF": ({"cooling_off": {"cancelled": False, "other_channel_attempts": 1}}, 30.0, 0),
}


def signals(env: Env, latency_s: float, switches: int = 0) -> ClientSignals:
    """The phone's timing for an answer given ``latency_s`` after it was shown; the server's
    clock moves on by the same time."""
    env.clock.advance(timedelta(seconds=latency_s))
    shown = 1_731_000_000_000
    return ClientSignals(shown, shown + round(latency_s * 1000), switches)


def drive(
    env: Env,
    svc: GuardianService,
    payment_id: UUID,
    answers: Mapping[str, tuple[dict[str, Any], float, int]],
) -> tuple[list[str], Decided]:
    """The whole session as the runner (#95) will drive it, answering each probe from the
    script. Returns the active probes in order and the decision."""
    started = svc.start(payment_id, mode=SessionMode.LIVE, runner=Runner.API)
    assert started.status is SessionStatus.CREATED
    asyncio.run(svc.passive(started.id))
    asked: list[str] = []
    while True:
        step = svc.plan(started.id)
        if isinstance(step, Decided):
            return asked, step
        issued = svc.issue(step.probe_run_id)
        assert isinstance(issued, IssuedProbe)
        asked.append(issued.probe_key)
        answer, latency, switches = answers[issued.probe_key]
        svc.respond(issued.probe_run_id, answer, signals(env, latency, switches))


def posterior(session_id: UUID, env: Env) -> dict[str, float]:
    with env.owner.connect() as conn:
        return dict(
            conn.execute(
                select(guardian_sessions.c.posterior).where(guardian_sessions.c.id == session_id)
            ).scalar_one()
        )


# ---------------------------------------------------------------- ENG-S1 / ENG-S2


def test_eng_s1_coached_victim_gets_the_spell_breaker_and_a_protective_hold(env: Env) -> None:
    svc = env.service(story_prior(KAMALA_PRIOR), KAMALA_PASSIVE, explore=False)
    payment = env.payment(480_000)
    asked, decided = drive(env, svc, payment, KAMALA_ANSWERS)
    assert asked[:2] == ["PURPOSE_QUESTION", "SPELL_BREAKER_DIGITAL_ARREST"]
    assert "OTP_STEP_UP" not in asked
    assert decided.session.decision == "PROTECTIVE_HOLD"
    p = posterior(decided.session.id, env)
    assert max(p, key=p.__getitem__) == "APP_DIGITAL_ARREST" and p["APP_DIGITAL_ARREST"] >= 0.65
    assert env.payment_row(payment).status == "HELD"
    assert decided.session.case_id is not None
    with env.sessions() as s:
        case = get_case(s, decided.session.case_id)
    assert case is not None and case.protocol.value == "COERCION"


def test_eng_s2_genuine_customer_on_a_new_phone_is_released_after_biometric(env: Env) -> None:
    svc = env.service(story_prior(ARJUN_PRIOR), ARJUN_PASSIVE, explore=False)
    payment = env.payment(180_000)
    asked, decided = drive(env, svc, payment, GENUINE_ANSWERS)
    assert asked[0] == "BIOMETRIC_STEP_UP"
    assert "OTP_STEP_UP" not in asked and len(asked) <= 2
    assert (decided.session.decision, decided.session.terminal_reason) == (
        "RELEASE",
        TerminalReason.STOP,
    )
    assert env.payment_row(payment).status == "EXECUTED"


# ---------------------------------------------------------------- issuing and answering


def _planning(env: Env, svc: GuardianService, payment: UUID) -> UUID:
    started = svc.start(payment, mode=SessionMode.LIVE, runner=Runner.API)
    asyncio.run(svc.passive(started.id))
    return started.id


def _pending(env: Env, session_id: UUID, probe_key: str) -> UUID:
    """A PENDING active run for ``probe_key``, as if the planner had chosen it."""
    with env.sessions.begin() as s:
        run = add_probe_run(s, env.clock, session_id, probe_key=probe_key, mode=ProbeMode.ACTIVE)
    return run.id


def _run(env: Env, run_id: UUID) -> Any:
    with env.owner.connect() as conn:
        return conn.execute(select(probe_runs).where(probe_runs.c.id == run_id)).one()


def test_a_question_is_served_rotated_and_classified(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE, q=1.0, p_mule=None), UNSURE, explore=False)
    first = env.payment(50_000)
    step = svc.plan(_planning(env, svc, first))
    assert isinstance(step, NextProbe) and step.probe_key == "PURPOSE_QUESTION"
    issued = svc.issue(step.probe_run_id)
    assert isinstance(issued, IssuedProbe) and issued.ui is Ui.QUESTION
    assert issued.question is not None and issued.question.language == "en"  # no te variant yet
    assert issued.timeout_s == 60  # kb guardrails.active_probe_response_timeout_s
    assert issued.session.status is SessionStatus.AWAITING_CUSTOMER
    run = _run(env, issued.probe_run_id)
    assert (run.status, run.variant_id) == ("AWAITING_CUSTOMER", issued.question.variant_id)

    done = svc.respond(issued.probe_run_id, {"option_key": "INVESTMENT"}, signals(env, 4.0))
    assert done.completed and done.outcome == "HIGH_RISK_PURPOSE"
    assert done.session.status is SessionStatus.PLANNING
    run = _run(env, issued.probe_run_id)
    assert run.status == "COMPLETED" and run.raw_response["latency_s"] == 4.0
    assert run.posterior_after == done.session.posterior and run.info_yield_bits > 0

    # the same customer's next session is shown the next variant (docs/07 §9)
    second = env.another_payment(first, 50_000)
    step = svc.plan(_planning(env, svc, second))
    assert isinstance(step, NextProbe) and step.probe_key == "PURPOSE_QUESTION"
    again = svc.issue(step.probe_run_id)
    assert isinstance(again, IssuedProbe) and again.question is not None
    assert again.question.variant_id != issued.question.variant_id


def test_bad_answers_and_timings_are_refused_and_leave_the_probe_waiting(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE, q=1.0, p_mule=None), UNSURE, explore=False)
    step = svc.plan(_planning(env, svc, env.payment(50_000)))
    assert isinstance(step, NextProbe)
    issued = svc.issue(step.probe_run_id)
    assert isinstance(issued, IssuedProbe)
    with pytest.raises(DelphiError) as bad_option:
        svc.respond(issued.probe_run_id, {"option_key": "LOTTERY"}, signals(env, 2.0))
    assert bad_option.value.code is ProblemCode.VALIDATION_FAILED
    # the phone claims 60 s, the server saw about 1 s: more than 10 s of slack
    env.clock.advance(timedelta(seconds=1))
    with pytest.raises(DelphiError) as implausible:
        svc.respond(
            issued.probe_run_id,
            {"option_key": "BILLS_OR_SHOPPING"},
            ClientSignals(1_000, 61_000),
        )
    assert implausible.value.code is ProblemCode.INVALID_CLIENT_SIGNALS
    with pytest.raises(DelphiError) as negative:
        svc.respond(issued.probe_run_id, {"option_key": "BILLS_OR_SHOPPING"}, ClientSignals(5, 1))
    assert negative.value.code is ProblemCode.INVALID_CLIENT_SIGNALS
    assert _run(env, issued.probe_run_id).status == "AWAITING_CUSTOMER"  # nothing was recorded
    with pytest.raises(DelphiError) as twice:
        svc.issue(issued.probe_run_id)
    assert twice.value.code is ProblemCode.INVALID_STATE_TRANSITION


def test_the_otp_protocol(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE), CLEAN, explore=False)
    session_id = _planning(env, svc, env.payment(480_000))
    issued = svc.issue(_pending(env, session_id, "OTP_STEP_UP"))
    assert isinstance(issued, IssuedProbe) and issued.ui is Ui.OTP
    code = issued.otp_code
    assert code is not None and len(code) == 6 and code.isdigit()
    run = _run(env, issued.probe_run_id)
    assert "otp_sha256" in run.raw_response and code not in run.raw_response.values()
    wrong = "000000" if code != "000000" else "111111"
    first = svc.respond(issued.probe_run_id, {"otp": wrong}, signals(env, 5.0))
    assert not first.completed and first.attempts_remaining == 2
    assert first.session.status is SessionStatus.AWAITING_CUSTOMER
    passed = svc.respond(issued.probe_run_id, {"otp": code}, signals(env, 5.0))
    assert passed.completed and passed.outcome == "PASSED"
    run = _run(env, issued.probe_run_id)
    assert run.raw_response["otp_attempts"] == 2
    assert run.raw_response["answer"] == {"otp": "******"}  # no code is kept, right or wrong
    assert code not in {v for v in run.raw_response.values() if isinstance(v, str)}


def test_three_wrong_codes_fail_the_otp(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE), CLEAN, explore=False)
    session_id = _planning(env, svc, env.payment(480_000))
    issued = svc.issue(_pending(env, session_id, "OTP_STEP_UP"))
    assert isinstance(issued, IssuedProbe) and issued.otp_code is not None
    wrong = "000000" if issued.otp_code != "000000" else "111111"
    for left in (2, 1):
        reply = svc.respond(issued.probe_run_id, {"otp": wrong}, signals(env, 2.0))
        assert reply.attempts_remaining == left
    last = svc.respond(issued.probe_run_id, {"otp": wrong}, signals(env, 2.0))
    assert last.completed and last.outcome == "FAILED_OR_ABANDONED"


# ---------------------------------------------------------------- timeouts and cancel


def test_a_step_up_timeout_fails_it_and_a_question_timeout_does_not_update(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE), CLEAN, explore=False)
    session_id = _planning(env, svc, env.payment(480_000))
    issued = svc.issue(_pending(env, session_id, "BIOMETRIC_STEP_UP"))
    assert isinstance(issued, IssuedProbe)
    after = svc.timeout(issued.probe_run_id)
    run = _run(env, issued.probe_run_id)
    assert (run.status, run.outcome) == ("COMPLETED", "FAILED_OR_ABANDONED")  # docs/05 §2
    assert run.posterior_after != run.posterior_before and after.status is SessionStatus.PLANNING

    question = svc.issue(_pending(env, session_id, "SPELL_BREAKER_INVESTMENT"))
    assert isinstance(question, IssuedProbe)
    before = posterior(session_id, env)
    svc.timeout(question.probe_run_id)
    run = _run(env, question.probe_run_id)
    assert (run.status, run.outcome) == ("TIMED_OUT", None)
    assert posterior(session_id, env) == before  # no update


def test_the_customer_cancels_during_a_probe(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE), CLEAN, explore=False)
    payment = env.payment(480_000)
    session_id = _planning(env, svc, payment)
    issued = svc.issue(_pending(env, session_id, "PURPOSE_QUESTION"))
    assert isinstance(issued, IssuedProbe)
    cancelled = svc.cancel(session_id)
    assert cancelled.status is SessionStatus.CANCELLED_BY_CUSTOMER
    assert cancelled.terminal_reason is TerminalReason.CUSTOMER_CANCELLED
    assert cancelled.decision is None and cancelled.decided_at is not None
    assert env.payment_row(payment).status == "CANCELLED"
    assert _run(env, issued.probe_run_id).status == "TIMED_OUT"
    with pytest.raises(DelphiError) as again:
        svc.cancel(session_id)
    assert again.value.code is ProblemCode.INVALID_STATE_TRANSITION


def test_cancelling_in_the_cooling_off_window_ends_the_session(env: Env) -> None:
    svc = env.service(FixedPrior(GENUINE), CLEAN, explore=False)
    payment = env.payment(480_000)
    session_id = _planning(env, svc, payment)
    issued = svc.issue(_pending(env, session_id, "COOLING_OFF"))
    assert isinstance(issued, IssuedProbe) and issued.ui is Ui.COOLING_OFF
    assert issued.timeout_s == 600 + 30  # window × time scale (1 outside DEMO) + extra
    reply = svc.respond(
        issued.probe_run_id, {"cooling_off": {"cancelled": True}}, signals(env, 20.0)
    )
    assert reply.outcome == "CANCELLED_BY_CUSTOMER"
    assert reply.session.status is SessionStatus.CANCELLED_BY_CUSTOMER
    assert env.payment_row(payment).status == "CANCELLED"
