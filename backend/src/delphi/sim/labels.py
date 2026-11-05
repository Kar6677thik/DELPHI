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
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from delphi.sim.response_models import CONFIG_DIR
from delphi.sim.rng import keyed_rng
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
    # Every fraud scenario is listed, even at 0.0, so a new or misspelled one cannot silently
    # go unreported.
    report_probability: dict[ScenarioType, Annotated[float, Field(ge=0, le=1)]]
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

    @model_validator(mode="after")
    def _every_fraud_scenario_has_a_report_probability(self) -> LabelConfig:
        missing = [
            s.value
            for s in ScenarioType
            if ground_truth_key(s) != GENUINE and s not in self.report_probability
        ]
        if missing:
            raise ValueError(f"report_probability has no entry for {', '.join(missing)}")
        return self

    def delay(self, median_hours: float, z: float) -> timedelta:
        """Log-normal: median · exp(sigma · z), for a standard normal draw z."""
        return timedelta(hours=median_hours * math.exp(self.label_delay_sigma * z))


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


# Which decisions each resolution can follow (docs/14 §7): DELPHI's outreach resolves a held or
# declined payment; an investigator disposes of a held one.
_RESOLVES = {
    LabelSource.OUTREACH_CONFIRMED: frozenset({PaymentDecision.HELD, PaymentDecision.DECLINED}),
    LabelSource.INVESTIGATOR_DISPOSITION: frozenset({PaymentDecision.HELD}),
}


def schedule_payment_labels(
    outcome: PaymentOutcome,
    config: LabelConfig,
    *,
    seed: int,
    maturity_days: int,
) -> list[ScheduledLabel]:
    """The labels a payment will receive, by docs/14 §7, in due order.

    The draws come from the payment's own stream, keyed by ``seed`` (the profile seed, docs/14
    §2) and its id, and both are made for every payment. So a payment gets the same report and
    delay draws whatever was decided for any other payment: policies being compared share
    their randomness (docs/22 §1). ``maturity_days`` is the profile's
    ``no_complaint_maturity_days``.
    """
    rng = keyed_rng(seed, "label", outcome.payment_id)
    report_u, delay_z = rng.random(), rng.standard_normal()

    def label(key: str, source: LabelSource, due: datetime) -> ScheduledLabel:
        return ScheduledLabel(SubjectType.PAYMENT, outcome.payment_id, key, source, due)

    if outcome.decision is not PaymentDecision.RELEASED:
        r = outcome.resolution
        if r is None:
            return []  # selective labels: nothing ever resolves it
        if outcome.decision not in _RESOLVES.get(r.source, frozenset()):
            msg = f"a {outcome.decision.value} payment cannot be resolved by {r.source.value}"
            raise ValueError(msg)
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
    if report_u >= config.report_probability[outcome.scenario]:
        return [matured]  # never reported: a wrong GENUINE label, by design
    complaint = label(
        truth,
        LabelSource.CUSTOMER_COMPLAINT,
        outcome.decided_at + config.delay(config.complaint_median_hours(outcome.scenario), delay_z),
    )
    # A complaint after maturity arrives after the bank has already labelled the payment
    # GENUINE; label precedence (docs/05 §6) then lets the complaint win.
    return [complaint] if complaint.due_at <= matured.due_at else [matured, complaint]


def schedule_registry_label(
    account_id: str,
    first_fraud_credit_at: datetime,
    config: LabelConfig,
    *,
    seed: int,
) -> ScheduledLabel:
    """A MULE label for an account the sim registry lists (docs/14 §4, §7), from the account's
    own stream. Which accounts are listed, true mules or false listings, is decided by the
    mule-network generator."""
    z = keyed_rng(seed, "registry", account_id).standard_normal()
    return ScheduledLabel(
        SubjectType.ACCOUNT,
        account_id,
        MULE,
        LabelSource.REGISTRY_LISTING,
        first_fraud_credit_at + config.delay(config.registry_delay_median_hours, z),
    )


def observed_hypothesis(label: ScheduledLabel, active_hypotheses: frozenset[str]) -> str:
    """The key the bank records at delivery: a complaint names the true hypothesis if it is in
    the ACTIVE KB at that time, else FRAUD_OTHER (docs/14 §7)."""
    complaint = label.label_source is LabelSource.CUSTOMER_COMPLAINT
    if complaint and label.hypothesis_key not in active_hypotheses:
        return FRAUD_OTHER
    return label.hypothesis_key
