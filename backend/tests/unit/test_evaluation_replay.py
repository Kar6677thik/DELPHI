"""The evaluation replay on `test` (docs/22 §1–2, #123 PR 1): the stream in time order, each
policy on its own copy of the bank, B1 on bank-visible views only, common random numbers for
the customer, and identical outcomes for identical seeds."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from delphi.evaluation.outcomes import Decision, FinalStatus
from delphi.evaluation.replay import B1, BankState, PolicyDecision, PolicyView, replay
from delphi.evaluation.stream import eval_stream, otp_would_pass, stream_truth
from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import VirtualClock
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.generator_config import load_generator_config
from delphi.sim.response_models import ResponseModels, load_response_models

SEED = 7


@pytest.fixture(scope="module")
def dataset(kb: KnowledgeBase) -> Dataset:
    return generate_dataset(load_generator_config(), "test", kb=kb)


@pytest.fixture(scope="module")
def models() -> ResponseModels:
    return load_response_models()


def _b1(kb: KnowledgeBase) -> B1:
    return B1(load_generator_config(), kb.config.classifiers.new_device_days)


def test_the_stream_is_the_windows_sessions_in_time_order(dataset: Dataset) -> None:
    stream = eval_stream(dataset)
    window = dataset.history.window
    assert window is not None and len(stream) == len(window.payments) > 100
    times = [i.payment.initiated_at for i in stream]
    assert times == sorted(times)
    assert all(t >= dataset.population.history_end for t in times)
    assert set(stream_truth(dataset)) == {i.payment.id for i in stream}


def test_b1_replays_identically_and_decides_every_payment(
    dataset: Dataset, kb: KnowledgeBase, models: ResponseModels
) -> None:
    first = replay(dataset, _b1(kb), seed=SEED, models=models)
    again = replay(dataset, _b1(kb), seed=SEED, models=models)
    assert first == again
    assert len(first) == len(eval_stream(dataset))
    decisions = {o.decision for o in first}
    assert Decision.RELEASE in decisions and Decision.HOLD_FOR_REVIEW in decisions
    for o in first:
        assert o.decided_at >= dataset.population.history_end
        held = o.final_status is FinalStatus.HELD
        assert held == (o.decision is Decision.HOLD_FOR_REVIEW) == (o.held_at is not None)
        assert o.probes == () and o.dd_pack_complete is None  # B1 asks nothing, builds no pack


class _Recorder:
    """A policy that releases everything and keeps the views it was shown."""

    name = "ALL_RELEASE"

    def __init__(self) -> None:
        self.views: list[PolicyView] = []

    def decide(self, view: PolicyView) -> PolicyDecision:
        self.views.append(view)
        return PolicyDecision(Decision.RELEASE, view.initiated_at)


def test_each_policy_has_its_own_bank(
    dataset: Dataset, kb: KnowledgeBase, models: ResponseModels
) -> None:
    """Releasing everything executes payments the other policy held: the later views differ
    (payees paid, balances), and neither run changes the other's."""
    recorder = _Recorder()
    released = replay(dataset, recorder, seed=SEED, models=models)
    b1 = replay(dataset, _b1(kb), seed=SEED, models=models)
    assert all(o.final_status is FinalStatus.EXECUTED for o in released)
    assert replay(dataset, _b1(kb), seed=SEED, models=models) == b1
    # Its own releases count as payments made: a pair first paid in the window is "paid" to
    # every later view of this run.
    first: dict[tuple[object, object], int] = {}
    for n, v in enumerate(recorder.views):
        first.setdefault((v.payer_account_id, v.payee_account_id), n)
    repeat = [
        v
        for n, v in enumerate(recorder.views)
        if first[(v.payer_account_id, v.payee_account_id)] < n
    ]
    assert repeat and all(v.payee_ever_paid for v in repeat)


def test_a_policy_sees_no_ground_truth() -> None:
    """#123 plan §5: every PolicyView field is bank-visible."""
    fields = {f.name for f in dataclasses.fields(PolicyView)}
    assert not fields & {"scenario", "is_fraud", "coached", "hypothesis_key", "otp_would_pass"}


def test_the_otp_draw_is_the_customers_not_the_policys(
    dataset: Dataset, models: ResponseModels
) -> None:
    truth = stream_truth(dataset)
    ids = sorted(truth)[:200]
    forward = [otp_would_pass(SEED, models, p, truth[p]) for p in ids]
    backward = [otp_would_pass(SEED, models, p, truth[p]) for p in reversed(ids)]
    assert forward == backward[::-1]  # the order of asking changes nothing
    assert any(forward)


def test_the_bank_state_executes_only_funded_transfers(dataset: Dataset) -> None:
    bank = BankState.at_history_end(dataset)
    payer, payee = next(iter(bank.paid))
    balance = bank.balances.get(payer, 0)
    assert not bank.transfer(payer, payee, balance + 1)
    assert bank.balances.get(payer, 0) == balance


def test_the_virtual_clock_only_moves_forward() -> None:
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    clock = VirtualClock(t0)
    clock.advance_to(t0 + timedelta(minutes=5))
    assert clock.now() == t0 + timedelta(minutes=5)
    with pytest.raises(ValueError):
        clock.advance_to(t0)
