"""What one policy did with each payment of the evaluation stream (docs/22 §1, #123 plan §4).

The harness (#123) fills these from its replay; the investigation queue (#124, docs/22 §3)
settles the holds (``final_status``, ``resolved_at``); `delphi.evaluation.metrics` reads them.
``scenario`` and ``otp_would_pass`` are ground truth, so these records stay inside
``delphi.evaluation``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from delphi.sim.scenarios import ScenarioType, ground_truth_key

GENUINE = "GENUINE"  # the one hypothesis that is no fraud


class Decision(StrEnum):
    """The policy's decision on the payment (docs/22 §2: HOLD is recorded as HOLD_FOR_REVIEW)."""

    RELEASE = "RELEASE"
    HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW"
    PROTECTIVE_HOLD = "PROTECTIVE_HOLD"
    DECLINE = "DECLINE"


class FinalStatus(StrEnum):
    """The payment's status at the end of the run, after the queue (docs/22 §4: "stopped" is
    anything but EXECUTED)."""

    EXECUTED = "EXECUTED"
    HELD = "HELD"
    DECLINED = "DECLINED"


class ProbeMode(StrEnum):
    PASSIVE = "PASSIVE"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    """One probe run on the payment: its friction cost (docs/07 §7) and, once observed, the
    information it yielded."""

    probe_key: str
    mode: ProbeMode
    friction_paise: int
    info_yield_bits: float | None


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    payment_id: UUID
    customer_id: UUID
    vulnerable: bool
    amount_paise: int
    scenario: ScenarioType  # sim truth
    otp_would_pass: bool  # sim truth, from the payment's own OTP stream (#123 plan §3)
    decision: Decision
    decided_at: datetime
    final_status: FinalStatus
    # When the payment entered the investigation queue, and when it left it. A Guardian
    # in-session resolution is no hold (docs/22 §4). A hold the run ends on has no resolved_at.
    held_at: datetime | None = None
    resolved_at: datetime | None = None
    probes: tuple[ProbeRecord, ...] = ()
    argmax_hypothesis: str | None = None
    dd_pack_complete: bool | None = None  # None: the policy builds no DD pack (B1, B2)

    @property
    def true_hypothesis(self) -> str:
        return ground_truth_key(self.scenario)

    @property
    def is_fraud(self) -> bool:
        """docs/22 §4: F is every payment whose scenario enacts a fraud hypothesis, the hidden
        UPI_COLLECT_REFUND (APP_COLLECT_REFUND) included; G is the rest."""
        return self.true_hypothesis != GENUINE

    @property
    def stopped(self) -> bool:
        return self.final_status is not FinalStatus.EXECUTED


@dataclass(frozen=True, slots=True)
class LabelRecord:
    """A label the bank learns about a payment, and when (docs/14 §7, #123 plan §4)."""

    payment_id: UUID
    source: str
    known_at: datetime
