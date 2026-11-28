"""A demo story's scripted prior (docs/06 §17, docs/14 §10, #291)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from delphi.engine import CompiledProfile
from delphi.guardian.procedure import session_prior
from delphi.guardian.story_prior import STORY_SCRIPT, StoryPrior, scripted_inputs
from delphi.sensing.api import PriorInputs
from delphi.sim import api as bank

from ..golden.test_story_behaviour import ARJUN_PRIOR, KAMALA_PRIOR

# The fixture tuples' order (tests/golden/test_story_behaviour.py, docs/06 §17).
ORDER = ("GENUINE", "ATO", "APP_DIGITAL_ARREST", "APP_INVESTMENT", "MULE", "UNEXPLAINED")
GOLDEN = {"S1": KAMALA_PRIOR, "S2": ARJUN_PRIOR}


@dataclass
class ModelPrior:
    calls: list[UUID] = field(default_factory=list)

    def prior_inputs(self, payment: bank.PaymentView) -> PriorInputs:
        self.calls.append(payment.id)
        return PriorInputs({"GENUINE": 0.99, "ATO": 0.01}, 0.3, 0.001)


def _payment(story: str | None) -> bank.PaymentView:
    return bank.PaymentView(
        id=uuid4(),
        payer_account_id=uuid4(),
        payee_account_id=uuid4(),
        payer_customer_id=uuid4(),
        amount_paise=48_000_000,
        channel="IMPS",
        initiated_at=datetime(2026, 11, 28, tzinfo=UTC),
        device_id=uuid4(),
        status="PENDING_GUARDIAN",
        story=story,
    )


def test_the_scripted_priors_are_the_golden_fixtures(guardian: CompiledProfile) -> None:
    """personas.yaml carries ENG-S1/S2's priors exactly, and their ∅ is KB v1's open mass of a
    payment that is not anomalous, so a live story starts where ENG-S1/S2 do."""
    scripted = bank.scripted_priors()
    assert set(scripted) == set(GOLDEN)
    for story, p0 in GOLDEN.items():
        assert scripted[story] == dict(zip(ORDER, p0, strict=True))
        assert scripted[story]["UNEXPLAINED"] == pytest.approx(guardian.open_mass_base)


@pytest.mark.parametrize("story", ["S1", "S2"])
def test_a_story_payment_starts_from_its_scripted_prior(
    guardian: CompiledProfile, story: str
) -> None:
    model = ModelPrior()
    inputs = StoryPrior(model, bank.scripted_priors()).prior_inputs(_payment(story))
    assert model.calls == []  # the trained model is not asked
    assert inputs.source == STORY_SCRIPT
    built = session_prior(guardian, inputs)
    prior = dict(zip(guardian.hypotheses, built.prior.tolist(), strict=True))
    expected = dict(zip(ORDER, GOLDEN[story], strict=True))
    for hypothesis, p in expected.items():
        assert prior[hypothesis] == pytest.approx(p, abs=1e-12)
    assert sum(prior.values()) == pytest.approx(1.0, abs=1e-12)


def test_every_other_payment_keeps_the_trained_model() -> None:
    model = ModelPrior()
    port = StoryPrior(model, bank.scripted_priors())
    for story in (None, "S3"):  # S-3 has no scripted prior
        payment = _payment(story)
        inputs = port.prior_inputs(payment)
        assert model.calls[-1] == payment.id
        assert inputs.source == "MODEL"


def test_the_scripted_inputs_name_their_story() -> None:
    inputs = scripted_inputs(dict(zip(ORDER, KAMALA_PRIOR, strict=True)), story="S1")
    assert inputs.top_features == (
        {"feature": "scripted_prior", "value": "S1", "contribution": 0.0},
    )
