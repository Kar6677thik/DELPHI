"""DELPHI and its ablations in the evaluation replay (docs/22 §2, #123 PR 3): the Guardian's own
procedure (``run_session``) on bank-visible views, with the customer answering from their own
stream."""

from __future__ import annotations

from dataclasses import replace

import pytest

from delphi.engine import compile_profile
from delphi.evaluation.delphi_policy import Delphi, passive_outcomes
from delphi.evaluation.outcomes import Decision, FinalStatus, PaymentOutcome, ProbeMode
from delphi.evaluation.replay import BankState, replay
from delphi.evaluation.sensing import HarnessSensing, train_and_score
from delphi.evaluation.stream import eval_stream
from delphi.kb.model import KnowledgeBase
from delphi.sensing.config import load_sensing_config
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.response_models import ResponseModels, load_response_models

SEED = 7
FALLBACK = 0.10


@pytest.fixture(scope="module")
def config() -> GeneratorConfig:
    return load_generator_config()


@pytest.fixture(scope="module")
def dataset(kb: KnowledgeBase, config: GeneratorConfig) -> Dataset:
    return generate_dataset(config, "test", kb=kb)


@pytest.fixture(scope="module")
def sensing(dataset: Dataset, kb: KnowledgeBase, config: GeneratorConfig) -> HarnessSensing:
    return train_and_score(dataset, kb, config)


@pytest.fixture(scope="module")
def models() -> ResponseModels:
    return load_response_models()


def _policy(kb: KnowledgeBase, sensing: HarnessSensing, kind: str = "full") -> Delphi:
    profile = compile_profile(kb, "GUARDIAN_PAYMENT")
    kw = {"sensing": sensing.inputs}
    if kind == "myopic":
        return Delphi.myopic(kb, profile, **kw)
    if kind == "no_active":
        return Delphi.no_active(kb, profile, **kw)
    return Delphi(kb, profile, **kw)


def test_delphi_replays_identically_and_decides_every_payment(
    dataset: Dataset, kb: KnowledgeBase, sensing: HarnessSensing, models: ResponseModels
) -> None:
    first = replay(dataset, _policy(kb, sensing), seed=SEED, models=models)
    again = replay(dataset, _policy(kb, sensing), seed=SEED, models=models)
    assert first == again
    assert len(first) == len(eval_stream(dataset))
    for o in first:
        assert o.argmax_hypothesis is not None
        assert (o.final_status is FinalStatus.HELD) == (
            o.decision in (Decision.HOLD_FOR_REVIEW, Decision.PROTECTIVE_HOLD)
        )
        passive = [p for p in o.probes if p.mode is ProbeMode.PASSIVE]
        assert all(p.friction_paise == 0 for p in passive)
        assert all(p.friction_paise > 0 for p in o.probes if p.mode is ProbeMode.ACTIVE)
    assert any(o.probes for o in first)  # most sessions take the passive probes


def test_the_ablations(
    dataset: Dataset, kb: KnowledgeBase, sensing: HarnessSensing, models: ResponseModels
) -> None:
    passive_only = replay(dataset, _policy(kb, sensing, "no_active"), seed=SEED, models=models)
    assert not any(p.mode is ProbeMode.ACTIVE for o in passive_only for p in o.probes)
    myopic = replay(dataset, _policy(kb, sensing, "myopic"), seed=SEED, models=models)
    assert len(myopic) == len(passive_only) == len(eval_stream(dataset))


def test_passive_outcomes_read_the_view_as_the_live_adapters_do(
    dataset: Dataset, kb: KnowledgeBase
) -> None:
    bank = BankState.at_history_end(dataset)
    item = next(i for i in eval_stream(dataset) if i.telemetry is not None)
    view = bank.view(item)
    assert item.telemetry is not None
    remote = replace(view, telemetry=replace(item.telemetry, remote_access_active=True))
    assert passive_outcomes(remote, kb, p_mule=FALLBACK)["DEVICE_POSTURE"] == "REMOTE_ACCESS_ACTIVE"
    listed = replace(view, payee_listed=True)
    assert passive_outcomes(listed, kb, p_mule=FALLBACK)["BENEFICIARY_INTEL"] == "LISTED_SUSPECT"
    unknown_device = replace(view, device_first_seen_at=None)
    assert passive_outcomes(unknown_device, kb, p_mule=FALLBACK)["DEVICE_POSTURE"] is None
    silent = replace(view, telemetry=None)
    assert passive_outcomes(silent, kb, p_mule=FALLBACK)["SESSION_BEHAVIOUR"] is None


def test_call_status_runs_only_with_consent_as_in_the_live_service(
    dataset: Dataset, kb: KnowledgeBase, sensing: HarnessSensing, models: ResponseModels
) -> None:
    """The live service drops a probe the planner finds statically ineligible (no call-status
    consent) before the passive phase, so it leaves no step and no ProbeRecord."""
    outcomes = replay(dataset, _policy(kb, sensing, "no_active"), seed=SEED, models=models)
    bank = BankState.at_history_end(dataset)

    def called(o: PaymentOutcome) -> bool:
        return any(p.probe_key == "CALL_STATUS" for p in o.probes)

    consenting = [o for o in outcomes if bank.customers[o.customer_id].consent_call_status]
    others = [o for o in outcomes if not bank.customers[o.customer_id].consent_call_status]
    assert consenting and others
    assert not any(called(o) for o in others)
    assert any(called(o) for o in consenting)


def test_the_mule_fallback_comes_from_the_sensing_settings(
    kb: KnowledgeBase, sensing: HarnessSensing
) -> None:
    policy = _policy(kb, sensing)
    assert policy.p_mule_fallback == load_sensing_config().features.mule_score_fallback
    assert policy.p_mule_fallback == FALLBACK  # docs/13 §6: 0.10
