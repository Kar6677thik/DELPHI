"""Sensing configuration, loaded from ``config.yaml`` next to this module (docs/13)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"


class AnomalyConfig(BaseModel):
    """The isolation forest and its percentile table (docs/13 §5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_estimators: int = Field(gt=0)
    contamination: Literal["auto"] | float
    random_state: int
    n_quantiles: int = Field(ge=2)


class FeatureConfig(BaseModel):
    """The payment features (docs/13 §2–3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    local_utc_offset_minutes: int = Field(ge=-12 * 60, le=14 * 60)
    amount_floor_inr: float = Field(gt=0)
    payee_age_cap_days: float = Field(gt=0)
    payee_account_age_cap_days: float = Field(gt=0)
    device_age_cap_days: float = Field(gt=0)
    new_device_days: float = Field(gt=0)
    new_payee_hours: float = Field(gt=0)
    baseline_days: int = Field(gt=0)
    channels: tuple[str, ...] = Field(min_length=1)
    limit_increase_event_types: tuple[str, ...] = ()
    mule_score_fallback: float = Field(ge=0, le=1)


class AccountFeatureConfig(BaseModel):
    """The account features (docs/13 §6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inbound_payers_hours: float = Field(gt=0)
    flow_days: float = Field(gt=0)
    device_share_days: float = Field(gt=0)
    account_age_cap_days: float = Field(gt=0)
    amount_floor_inr: float = Field(gt=0)
    cash_out_channel: str
    minimal_kyc_level: str
    executed_status: str


class SplitParts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    train: int = Field(gt=0)
    validation: int = Field(gt=0)
    test: int = Field(gt=0)


class PriorGate(BaseModel):
    """docs/13 §4: the quality gate, enforced only on the listed profiles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_ece: float = Field(gt=0, le=1)
    min_macro_auc: float = Field(ge=0, le=1)
    enforced_profiles: tuple[str, ...]


class PriorConfig(BaseModel):
    """The LightGBM multiclass prior model (docs/13 §4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classes: tuple[str, ...] = Field(min_length=2)
    num_leaves: int = Field(gt=1)
    learning_rate: float = Field(gt=0)
    n_estimators: int = Field(gt=0)
    early_stopping_rounds: int = Field(gt=0)
    seed: int
    split_parts: SplitParts
    temperature_bounds: tuple[float, float]
    ece_bins: int = Field(gt=0)
    top_contributions: int = Field(gt=0)
    label_precedence: tuple[str, ...] = Field(min_length=1)
    gate: PriorGate

    @model_validator(mode="after")
    def _check(self) -> PriorConfig:
        if len(set(self.classes)) != len(self.classes):
            raise ValueError("prior classes must be distinct")
        low, high = self.temperature_bounds
        if not 0 < low < 1 < high:
            raise ValueError("temperature_bounds must bracket 1 and be positive")
        return self


class SensingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    anomaly: AnomalyConfig
    prior: PriorConfig
    features: FeatureConfig
    accounts: AccountFeatureConfig


def load_sensing_config(path: Path = CONFIG_FILE) -> SensingConfig:
    return SensingConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
