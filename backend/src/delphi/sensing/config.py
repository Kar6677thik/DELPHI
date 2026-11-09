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


class SensingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    anomaly: AnomalyConfig


def load_sensing_config(path: Path = CONFIG_FILE) -> SensingConfig:
    return SensingConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
