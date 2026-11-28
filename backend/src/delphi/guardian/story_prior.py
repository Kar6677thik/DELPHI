"""A demo story's scripted prior (docs/06 §17, docs/14 §10, #291).

The trained prior gives a demo persona's story payment a few tenths of a percent of its story's
hypothesis, and that share swings ~15x from one training to the next: a live story's path was a
coin flip (#291, owner decision 2026-11-28). A story payment (``bank.payments.story``) therefore
starts from its story's scripted prior, the docs/06 §17 fixture that ENG-S1/S2 pin
(``sim/config/personas.yaml`` ``scripted_prior``). Every other payment keeps the sensing
layer's. The session records which (``prior_source``), and every view says so.

The scripted prior is given over (G, ATO, DA, INV, MULE, ∅); it is handed to the engine as the
sensing inputs that rebuild it exactly (docs/06 §11): MULE as the payer's mule score, ∅ as the
open mass of a payment that is not anomalous (``open_mass_base``), the rest as ``p_payment``.
"""

from __future__ import annotations

from collections.abc import Mapping

from delphi.guardian.service import PriorPort
from delphi.sensing.api import PriorInputs
from delphi.sim import api as bank

MULE = "MULE"
UNEXPLAINED = "UNEXPLAINED"
STORY_SCRIPT = "STORY_SCRIPT"
# An anomaly percentile below the open mass's knee (docs/06 §11, 0.90): the open mass is then
# `open_mass_base`, which the scripted ∅ equals (a test pins it against KB v1).
NOT_ANOMALOUS = 0.5


class StoryPrior:
    """The Guardian's ``PriorPort``: a story payment's scripted prior, else ``inner``'s."""

    def __init__(self, inner: PriorPort, scripted: Mapping[str, Mapping[str, float]]) -> None:
        self._inner = inner
        self._scripted = {story: dict(p0) for story, p0 in scripted.items()}

    def prior_inputs(self, payment: bank.PaymentView) -> PriorInputs:
        p0 = self._scripted.get(payment.story or "")
        if p0 is None:
            return self._inner.prior_inputs(payment)
        return scripted_inputs(p0, story=str(payment.story))


def scripted_inputs(p0: Mapping[str, float], *, story: str) -> PriorInputs:
    """The sensing inputs that rebuild ``p0`` exactly (docs/06 §11)."""
    u = p0[UNEXPLAINED]
    p_mule = p0[MULE] / (1 - u)
    scale = (1 - u) * (1 - p_mule)
    p_payment = {h: p / scale for h, p in p0.items() if h not in (MULE, UNEXPLAINED)}
    why = ({"feature": "scripted_prior", "value": story, "contribution": 0.0},)
    return PriorInputs(p_payment, NOT_ANOMALOUS, p_mule, why, source=STORY_SCRIPT)
