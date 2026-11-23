"""The simulated investigation queue of docs/22 §3 on hand-built micro-streams (#124 part 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from delphi.engine import CompiledProfile, compile_profile
from delphi.evaluation.config import QueueConfig, load_evaluation_config
from delphi.evaluation.outcomes import Decision, FinalStatus, PaymentOutcome
from delphi.evaluation.queue import (
    CONFIRM_FRAUD,
    OUTREACH,
    RELEASE,
    EvidenceOracle,
    HeldPayment,
    Investigation,
    Resolution,
    Shifts,
    by_sla_then_expected_loss,
    checklist,
    evidence_keys,
    fifo,
    navigator,
    run_queue,
    settle,
)
from delphi.kb.model import KnowledgeBase
from delphi.sim.scenarios import ScenarioType

IST = timezone(timedelta(hours=5, minutes=30))
NINE = datetime(2026, 11, 2, 9, 0, tzinfo=IST)  # a shift start
CONFIG = load_evaluation_config().queue
GENUINE_BELIEF = {"GENUINE": 0.7, "ATO": 0.05, "APP_DIGITAL_ARREST": 0.1, "APP_INVESTMENT": 0.1}
COERCED_BELIEF = {"GENUINE": 0.3, "APP_DIGITAL_ARREST": 0.5, "APP_INVESTMENT": 0.1, "MULE": 0.1}


@pytest.fixture(scope="module")
def case(kb: KnowledgeBase) -> CompiledProfile:
    return compile_profile(kb, "CASE_PAYMENT_HOLD")


def oracle_for(profile: CompiledProfile, hypothesis: str) -> EvidenceOracle:
    """Every source answers with its likeliest outcome under ``hypothesis``."""
    h = profile.hypotheses.index(hypothesis)

    def oracle(_: UUID, key: str) -> str:
        src = profile.sources[key]
        return src.outcomes[int(src.likelihood[:, h].argmax())]

    return oracle


def held(
    n: int, at: datetime, decision: Decision = Decision.HOLD_FOR_REVIEW, **kw: object
) -> HeldPayment:
    return HeldPayment(
        payment_id=UUID(int=n),
        decision=decision,
        held_at=at,
        amount_paise=int(kw.get("amount", 5_000_000)),  # type: ignore[call-overload]
        vulnerable=False,
        belief=kw.get("belief", GENUINE_BELIEF),  # type: ignore[arg-type]
    )


def fixed(minutes: float, disposition: str = RELEASE) -> Investigation:
    return Investigation(disposition, minutes, minutes, 0.0, ())


# --------------------------------------------------------------------------- working time


def test_work_waits_for_the_shift_and_carries_over_to_the_next() -> None:
    shifts = Shifts(CONFIG)
    assert shifts.on_shift(NINE - timedelta(hours=3)) == NINE
    assert shifts.on_shift(NINE + timedelta(hours=8)) == NINE + timedelta(days=1)  # 17:00
    assert shifts.on_shift(NINE + timedelta(hours=2)) == NINE + timedelta(hours=2)
    late = NINE + timedelta(hours=7, minutes=50)  # 16:50
    assert shifts.add_work(late, 20) == NINE + timedelta(days=1, minutes=10)
    assert shifts.add_work(late, 10) == NINE + timedelta(hours=8)  # ends exactly at 17:00


# --------------------------------------------------------------------------- the queue


def test_two_investigators_take_fifo_cases_as_they_free_up() -> None:
    items = [held(n, NINE) for n in (1, 2, 3)]
    out = run_queue(items, lambda _: fixed(16.5), fifo, CONFIG, until=NINE + timedelta(days=1))
    assert out[UUID(int=1)].started_at == out[UUID(int=2)].started_at == NINE
    third = out[UUID(int=3)]
    assert third.started_at == NINE + timedelta(minutes=16.5)
    assert third.resolved_at == NINE + timedelta(minutes=33)


def test_a_hold_off_shift_waits_for_the_morning() -> None:
    evening = NINE + timedelta(hours=11)  # 20:00
    out = run_queue(
        [held(1, evening)], lambda _: fixed(5), fifo, CONFIG, until=evening + timedelta(days=2)
    )
    assert out[UUID(int=1)].started_at == NINE + timedelta(days=1)


def test_delphi_serves_the_soonest_sla_first(case: CompiledProfile, kb: KnowledgeBase) -> None:
    """A protective hold (30-minute SLA) jumps a standard hold (240 minutes) held earlier."""
    one = QueueConfig(**{**CONFIG.model_dump(), "investigators": 1})
    standard = held(1, NINE)
    protective = held(2, NINE + timedelta(minutes=1), Decision.PROTECTIVE_HOLD)
    blocker = held(0, NINE - timedelta(minutes=1))  # keeps the investigator busy until both wait
    order = by_sla_then_expected_loss(case, kb)
    out = run_queue(
        [standard, protective, blocker],
        lambda _: fixed(10),
        order,
        one,
        until=NINE + timedelta(days=1),
    )
    assert out[UUID(int=2)].started_at < out[UUID(int=1)].started_at
    fifo_out = run_queue(
        [standard, protective, blocker],
        lambda _: fixed(10),
        fifo,
        one,
        until=NINE + timedelta(days=1),
    )
    assert fifo_out[UUID(int=1)].started_at < fifo_out[UUID(int=2)].started_at


def test_delphi_breaks_an_sla_tie_by_the_larger_expected_loss(
    case: CompiledProfile, kb: KnowledgeBase
) -> None:
    order = by_sla_then_expected_loss(case, kb)
    small = held(1, NINE, belief=COERCED_BELIEF, amount=100_000)
    large = held(2, NINE, belief=COERCED_BELIEF, amount=10_000_000)
    assert order(large) < order(small)


def test_the_run_ends_with_unstarted_and_unfinished_cases() -> None:
    one = QueueConfig(**{**CONFIG.model_dump(), "investigators": 1})
    items = [held(1, NINE), held(2, NINE + timedelta(seconds=1))]
    out = run_queue(items, lambda _: fixed(60), fifo, one, until=NINE + timedelta(minutes=30))
    assert out[UUID(int=1)].resolved_at is None  # still being worked
    assert UUID(int=2) not in out  # never started


# --------------------------------------------------------------------------- handling


def test_the_baseline_checklist_fetches_all_nine_items(
    case: CompiledProfile, kb: KnowledgeBase
) -> None:
    assert len(evidence_keys(case)) == 9
    genuine = checklist(case, kb, oracle_for(case, "GENUINE"), CONFIG)(held(1, NINE))
    assert genuine.evidence_minutes == 13.5  # docs/22 §3
    assert genuine.minutes == 13.5 + CONFIG.dispose_minutes
    assert genuine.disposition == RELEASE
    assert genuine.entropy_reduced_bits > 0
    coerced = checklist(case, kb, oracle_for(case, "APP_DIGITAL_ARREST"), CONFIG)(
        held(1, NINE, belief=COERCED_BELIEF)
    )
    assert coerced.disposition == CONFIRM_FRAUD


def test_delphi_calls_first_under_coercion_and_stops_early(
    case: CompiledProfile, kb: KnowledgeBase
) -> None:
    always = QueueConfig(**{**CONFIG.model_dump(), "accept_probability": 1.0})
    item = held(1, NINE, Decision.PROTECTIVE_HOLD, belief=COERCED_BELIEF)
    done = navigator(case, kb, oracle_for(case, "APP_DIGITAL_ARREST"), always, seed=7)(item)
    assert done.outcomes[0] == (OUTREACH, "CONFIRMS_COERCION")
    assert done.disposition == CONFIRM_FRAUD
    assert done.evidence_minutes < 13.5  # the planner stops before the whole checklist
    never = QueueConfig(**{**CONFIG.model_dump(), "accept_probability": 0.0})
    overruled = navigator(case, kb, oracle_for(case, "APP_DIGITAL_ARREST"), never, seed=7)(item)
    assert overruled.disposition == RELEASE  # the investigator takes the other decision
    assert overruled.outcomes == done.outcomes


# --------------------------------------------------------------------------- settling


def _outcome(n: int, decision: Decision) -> PaymentOutcome:
    return PaymentOutcome(
        payment_id=UUID(int=n),
        customer_id=UUID(int=100),
        vulnerable=False,
        amount_paise=1_000,
        scenario=ScenarioType.APP_DIGITAL_ARREST,
        otp_would_pass=True,
        decision=decision,
        decided_at=NINE.astimezone(UTC),
        final_status=FinalStatus.HELD,
    )


def test_settling_applies_the_queue_and_its_labels() -> None:
    later = NINE + timedelta(minutes=20)
    called = Investigation(CONFIRM_FRAUD, 11.0, 8.0, 1.5, ((OUTREACH, "CONFIRMS_COERCION"),))
    resolutions = {
        UUID(int=1): Resolution(NINE, later, called),
        UUID(int=2): Resolution(NINE, later, fixed(5)),
        UUID(int=3): Resolution(NINE, None, fixed(5)),
    }
    outcomes = [
        _outcome(1, Decision.PROTECTIVE_HOLD),
        _outcome(2, Decision.HOLD_FOR_REVIEW),
        _outcome(3, Decision.HOLD_FOR_REVIEW),
        _outcome(4, Decision.HOLD_FOR_REVIEW),  # never reached
        _outcome(5, Decision.DECLINE),  # not queued
    ]
    settled, labels = settle(outcomes, resolutions)
    by_id = {o.payment_id.int: o for o in settled}
    assert by_id[1].final_status is FinalStatus.DECLINED and by_id[1].resolved_at == later
    assert (by_id[1].evidence_minutes, by_id[1].entropy_reduced_bits) == (8.0, 1.5)
    assert by_id[2].final_status is FinalStatus.EXECUTED
    for n in (3, 4):
        assert by_id[n].final_status is FinalStatus.HELD and by_id[n].resolved_at is None
        assert by_id[n].held_at == by_id[n].decided_at
    assert by_id[5] == outcomes[4]
    assert sorted((label.payment_id.int, label.source) for label in labels) == [
        (1, "INVESTIGATOR_DISPOSITION"),
        (1, "OUTREACH_CONFIRMED"),
        (2, "INVESTIGATOR_DISPOSITION"),
    ]
