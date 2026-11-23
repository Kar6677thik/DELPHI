"""B2 in the evaluation replay (docs/22 §2, #123 PR 2): the harness trains the sensing prior in
memory on the history's observed labels, scores the stream once, and B2 decides from the same
prior the Guardian builds, asking for an OTP in its middle band."""

from __future__ import annotations

from uuid import UUID

import pytest

from delphi.engine import compile_profile
from delphi.evaluation.outcomes import Decision, FinalStatus
from delphi.evaluation.replay import B2, replay
from delphi.evaluation.sensing import HarnessSensing, train_and_score
from delphi.evaluation.stream import eval_stream, otp_would_pass, stream_truth
from delphi.guardian.api import PriorInputs
from delphi.kb.model import KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.response_models import ResponseModels, load_response_models

SEED = 7


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


def test_every_stream_payment_is_scored_as_of_its_initiation(
    dataset: Dataset, sensing: HarnessSensing
) -> None:
    stream = {i.payment.id for i in eval_stream(dataset)}
    assert set(sensing.inputs) == stream
    for inputs in sensing.inputs.values():
        assert sum(inputs.p_payment.values()) == pytest.approx(1.0)
        assert 0.0 <= inputs.anomaly_percentile <= 1.0
        assert inputs.p_mule_payer is None


def test_b2_follows_the_score_bands_and_the_customers_own_otp(
    dataset: Dataset,
    kb: KnowledgeBase,
    config: GeneratorConfig,
    sensing: HarnessSensing,
    models: ResponseModels,
) -> None:
    profile = compile_profile(kb, "GUARDIAN_PAYMENT")
    b2 = B2(config, profile, sensing.inputs)
    outcomes = replay(dataset, b2, seed=SEED, models=models)
    assert replay(dataset, b2, seed=SEED, models=models) == outcomes  # deterministic
    truth = stream_truth(dataset)
    for o in outcomes:
        if o.probes:
            [otp] = o.probes
            assert otp.probe_key == "OTP_STEP_UP" and otp.friction_paise > 0
            # The OTP band releases exactly when this customer's OTP draw passes.
            passes = otp_would_pass(SEED, models, o.payment_id, truth[o.payment_id])
            assert (o.decision is Decision.RELEASE) == passes
        else:
            assert o.decision in (Decision.RELEASE, Decision.HOLD_FOR_REVIEW)
        assert (o.final_status is FinalStatus.HELD) == (o.decision is Decision.HOLD_FOR_REVIEW)
    assert {o.decision for o in outcomes} <= {Decision.RELEASE, Decision.HOLD_FOR_REVIEW}


@pytest.mark.parametrize(
    ("p_genuine", "decision", "asks"),
    [(0.95, Decision.RELEASE, False), (0.2, Decision.HOLD_FOR_REVIEW, False)],
)
def test_b2_bands(
    dataset: Dataset,
    kb: KnowledgeBase,
    config: GeneratorConfig,
    models: ResponseModels,
    p_genuine: float,
    decision: Decision,
    asks: bool,
) -> None:
    """With every payment scored alike, B2 makes the same decision everywhere (the OTP band is
    covered above)."""
    profile = compile_profile(kb, "GUARDIAN_PAYMENT")
    rest = (1 - p_genuine) / 3
    flat = PriorInputs(
        {"GENUINE": p_genuine, "ATO": rest, "APP_DIGITAL_ARREST": rest, "APP_INVESTMENT": rest},
        anomaly_percentile=0.0,
        p_mule_payer=None,
    )
    inputs: dict[UUID, PriorInputs] = {i.payment.id: flat for i in eval_stream(dataset)}
    outcomes = replay(dataset, B2(config, profile, inputs), seed=SEED, models=models)
    assert {o.decision for o in outcomes} == {decision}
    assert all(bool(o.probes) == asks for o in outcomes)


def test_the_otp_band_releases_exactly_when_the_customers_otp_passes(
    dataset: Dataset, kb: KnowledgeBase, config: GeneratorConfig, models: ResponseModels
) -> None:
    """r = 0.4 puts every payment in B2's OTP band: each asks once, and releases if and only if
    its customer's own OTP draw passes (the same draw the coercion metric reads)."""
    profile = compile_profile(kb, "GUARDIAN_PAYMENT")
    flat = PriorInputs(
        {"GENUINE": 0.6, "ATO": 0.2, "APP_DIGITAL_ARREST": 0.1, "APP_INVESTMENT": 0.1},
        anomaly_percentile=0.0,
        p_mule_payer=None,
    )
    inputs = {i.payment.id: flat for i in eval_stream(dataset)}
    outcomes = replay(dataset, B2(config, profile, inputs), seed=SEED, models=models)
    truth = stream_truth(dataset)
    assert all(len(o.probes) == 1 for o in outcomes)
    released = [o.decision is Decision.RELEASE for o in outcomes]
    passes = [otp_would_pass(SEED, models, o.payment_id, truth[o.payment_id]) for o in outcomes]
    assert released == passes
    assert any(released) and not all(released)
