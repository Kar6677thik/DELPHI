"""The evaluation stream and its ground truth (docs/22 §1, #123 plan §1, §3, §5).

The stream is the window's undecided customer sessions in time order, each with its session
telemetry: what the Guardian sees at initiation. The truth (each payment's scenario and whether
its victim is coached) stays here and in ``outcomes``: policies see only a ``PolicyView``.

**Common random numbers.** Every random quantity a customer produces comes from a stream keyed
by the payment and what is drawn (``customer_rng``), never from a policy's own stream. So a
customer gives the same answer to the same probe under every policy, and the run does not
depend on how many workers share it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

import numpy as np

from delphi.sim.dataset import Dataset
from delphi.sim.history import Payment
from delphi.sim.response_models import ResponseModels
from delphi.sim.rng import keyed_rng
from delphi.sim.scenarios import ScenarioType
from delphi.sim.telemetry import SessionTelemetry

OTP_STEP_UP = "OTP_STEP_UP"
OTP_PASSED = "PASSED"


@dataclass(frozen=True, slots=True)
class StreamItem:
    payment: Payment
    telemetry: SessionTelemetry | None


@dataclass(frozen=True, slots=True)
class PaymentTruth:
    scenario: ScenarioType
    coached: bool


def eval_stream(dataset: Dataset) -> tuple[StreamItem, ...]:
    """The window's sessions in initiation order (ties by payment id)."""
    window = dataset.history.window
    if window is None:
        return ()
    telemetry = {t.payment_id: t for t in window.telemetry}
    ordered = sorted(window.payments, key=lambda p: (p.initiated_at, p.id))
    return tuple(StreamItem(p, telemetry.get(p.id)) for p in ordered)


def stream_truth(dataset: Dataset) -> Mapping[UUID, PaymentTruth]:
    """Each stream payment's scenario and coaching (sim truth)."""
    window = dataset.history.window
    if window is None:
        return {}
    kinds = {i.id: ScenarioType(i.scenario_type) for i in dataset.history.scenario_instances}
    return {
        t.payment_id: PaymentTruth(kinds[t.scenario_instance_id], t.coached)
        for t in window.payment_truth
    }


def customer_rng(seed: int, payment_id: UUID, *what: str) -> np.random.Generator:
    """The customer's own stream for one quantity of one payment (#123 plan §3)."""
    return keyed_rng(seed, "eval", str(payment_id), *what)


def otp_would_pass(
    seed: int, models: ResponseModels, payment_id: UUID, truth: PaymentTruth
) -> bool:
    """Whether the customer would pass an OTP step-up on this payment: the same draw for every
    policy that asks (B2, DELPHI) and for the coercion-catch metric (docs/22 §4)."""
    rng = customer_rng(seed, payment_id, OTP_STEP_UP)
    outcome = models.sample_answer(rng, truth.scenario, OTP_STEP_UP, coached=truth.coached)
    return outcome == OTP_PASSED
