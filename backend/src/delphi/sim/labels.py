"""The label arrival model (docs/14 §7, FR-SIM-04): which labels the bank observes, and when.

Pure functions. The scenario generator calls them once per payment (or mule account) and stores
the result in ``sim_truth.label_schedule``; the ``LABEL_DELIVERY`` job delivers each label when
it falls due (docs/14 §8). Two properties are deliberate:

- **Selective labels.** A payment held or declined without a resolution never gets a label.
- **Label noise.** Released fraud that nobody reports matures as GENUINE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field

from delphi.sim.response_models import CONFIG_DIR
from delphi.sim.scenarios import ScenarioType, ground_truth_key

LABELS_CONFIG_FILE = CONFIG_DIR / "labels.yaml"

GENUINE = "GENUINE"
MULE = "MULE"
# Confirmed fraud of a type the active KB does not model (docs/05 §6).
FRAUD_OTHER = "FRAUD_OTHER"


class LabelSource(StrEnum):
    CUSTOMER_COMPLAINT = "CUSTOMER_COMPLAINT"
    NO_COMPLAINT_60D = "NO_COMPLAINT_60D"
    OUTREACH_CONFIRMED = "OUTREACH_CONFIRMED"
    INVESTIGATOR_DISPOSITION = "INVESTIGATOR_DISPOSITION"
    REGISTRY_LISTING = "REGISTRY_LISTING"


class SubjectType(StrEnum):
    PAYMENT = "PAYMENT"
    ACCOUNT = "ACCOUNT"


class PaymentDecision(StrEnum):
    RELEASED = "RELEASED"
    HELD = "HELD"
    DECLINED = "DECLINED"


# --------------------------------------------------------------------------- configuration


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LabelConfig(_Frozen):
    report_probability: dict[ScenarioType, float]
    label_delay_sigma: float = Field(gt=0)
    complaint_delay_median_hours: float = Field(gt=0)
    complaint_delay_median_hours_by_scenario: dict[ScenarioType, float] = Field(
        default_factory=dict
    )
    registry_delay_median_hours: float = Field(gt=0)

    def complaint_median_hours(self, scenario: ScenarioType) -> float:
        return self.complaint_delay_median_hours_by_scenario.get(
            scenario, self.complaint_delay_median_hours
        )

    def sample_delay(self, rng: np.random.Generator, median_hours: float) -> timedelta:
        """Log-normal: median · exp(sigma · z), z ~ N(0, 1)."""
        return timedelta(
            hours=median_hours * math.exp(self.label_delay_sigma * rng.standard_normal())
        )


def load_label_config(path: Path = LABELS_CONFIG_FILE) -> LabelConfig:
    return LabelConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- the model


@dataclass(frozen=True, slots=True)
class Resolution:
    """What resolves a held or declined payment: DELPHI's outreach or an investigator."""

    source: LabelSource  # OUTREACH_CONFIRMED or INVESTIGATOR_DISPOSITION
    hypothesis_key: str
    at: datetime


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    payment_id: str
    scenario: ScenarioType
    decision: PaymentDecision
    decided_at: datetime
    resolution: Resolution | None = None


@dataclass(frozen=True, slots=True)
class ScheduledLabel:
    """A row of ``sim_truth.label_schedule`` (docs/17). ``hypothesis_key`` is the ground truth;
    ``observed_hypothesis`` maps it to what the bank records at delivery."""

    subject_type: SubjectType
    subject_id: str
    hypothesis_key: str
    label_source: LabelSource
    due_at: datetime


_RESOLUTION_SOURCES = frozenset(
    {LabelSource.OUTREACH_CONFIRMED, LabelSource.INVESTIGATOR_DISPOSITION}
)


def schedule_payment_labels(
    outcome: PaymentOutcome,
    rng: np.random.Generator,
    config: LabelConfig,
    *,
    maturity_days: int,
) -> list[ScheduledLabel]:
    """The labels a payment will receive, by docs/14 §7, in due order.

    ``maturity_days`` is the profile's ``no_complaint_maturity_days`` (docs/14 §2).
    """

    def label(key: str, source: LabelSource, due: datetime) -> ScheduledLabel:
        return ScheduledLabel(SubjectType.PAYMENT, outcome.payment_id, key, source, due)

    if outcome.decision is not PaymentDecision.RELEASED:
        if outcome.resolution is None:
            return []  # selective labels: nothing ever resolves it
        if outcome.resolution.source not in _RESOLUTION_SOURCES:
            source = outcome.resolution.source
            msg = f"a hold is resolved by outreach or an investigator, not {source}"
            raise ValueError(msg)
        r = outcome.resolution
        return [label(r.hypothesis_key, r.source, r.at)]
    if outcome.resolution is not None:
        raise ValueError("a released payment has no hold to resolve")

    matured = label(
        GENUINE,
        LabelSource.NO_COMPLAINT_60D,
        outcome.decided_at + timedelta(days=maturity_days),
    )
    truth = ground_truth_key(outcome.scenario)
    if truth == GENUINE:
        return [matured]
    if rng.random() >= config.report_probability.get(outcome.scenario, 0.0):
        return [matured]  # never reported: a wrong GENUINE label, by design
    complaint = label(
        truth,
        LabelSource.CUSTOMER_COMPLAINT,
        outcome.decided_at
        + config.sample_delay(rng, config.complaint_median_hours(outcome.scenario)),
    )
    # A complaint after maturity arrives after the bank has already labelled the payment
    # GENUINE; label precedence (docs/05 §6) then lets the complaint win.
    return [complaint] if complaint.due_at <= matured.due_at else [matured, complaint]


def schedule_registry_label(
    account_id: str,
    first_fraud_credit_at: datetime,
    rng: np.random.Generator,
    config: LabelConfig,
) -> ScheduledLabel:
    """A MULE label for an account the sim registry lists (docs/14 §4, §7). Which accounts are
    listed, true mules or false listings, is decided by the mule-network generator."""
    return ScheduledLabel(
        SubjectType.ACCOUNT,
        account_id,
        MULE,
        LabelSource.REGISTRY_LISTING,
        first_fraud_credit_at + config.sample_delay(rng, config.registry_delay_median_hours),
    )


def observed_hypothesis(label: ScheduledLabel, active_hypotheses: frozenset[str]) -> str:
    """The key the bank records at delivery: a complaint names the true hypothesis if it is in
    the ACTIVE KB at that time, else FRAUD_OTHER (docs/14 §7)."""
    complaint = label.label_source is LabelSource.CUSTOMER_COMPLAINT
    if complaint and label.hypothesis_key not in active_hypotheses:
        return FRAUD_OTHER
    return label.hypothesis_key
