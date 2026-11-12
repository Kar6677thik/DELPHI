"""Sensing configuration, loaded from ``config.yaml`` next to this module (docs/13)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

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


class SensingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    anomaly: AnomalyConfig
    features: FeatureConfig


def load_sensing_config(path: Path = CONFIG_FILE) -> SensingConfig:
    return SensingConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
