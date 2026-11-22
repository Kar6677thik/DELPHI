"""Evaluation configuration, loaded from ``config.yaml`` next to this module (docs/22)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"


class BootstrapConfig(BaseModel):
    """The cluster bootstrap over customers (docs/22 §1 step 7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    replicates: int = Field(gt=0)
    level: float = Field(gt=0, lt=1)
    seed: int


class QueueConfig(BaseModel):
    """The simulated investigation queue (docs/22 §3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    investigators: int = Field(gt=0)
    shift_start_hour: int = Field(ge=0, lt=24)
    shift_end_hour: int = Field(gt=0, le=24)
    local_utc_offset_minutes: int = Field(ge=-12 * 60, le=14 * 60)
    dispose_minutes: float = Field(ge=0)
    accept_probability: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _shift(self) -> QueueConfig:
        if self.shift_end_hour <= self.shift_start_hour:
            raise ValueError("the shift must end after it starts")
        return self


class TargetsConfig(BaseModel):
    """The pre-registered targets (docs/22 §5). Never tuned to the results."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    genuine_hold_reduction_vs_b1: float = Field(gt=0, le=1)
    coercion_catch_multiple_vs_b2: float = Field(gt=0)
    median_resolution_fraction_of_b1: float = Field(gt=0, le=1)
    min_label_coverage_held: float = Field(gt=0, le=1)
    golden_hour_multiple_vs_manual: float = Field(gt=0)
    dd_completeness: float = Field(gt=0, le=1)


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bootstrap: BootstrapConfig
    label_coverage_days: float = Field(gt=0)
    queue: QueueConfig
    targets: TargetsConfig


def load_evaluation_config(path: Path = CONFIG_FILE) -> EvaluationConfig:
    with path.open(encoding="utf-8") as f:
        return EvaluationConfig.model_validate(yaml.safe_load(f))
