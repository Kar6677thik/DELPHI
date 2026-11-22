"""Evaluation configuration, loaded from ``config.yaml`` next to this module (docs/22)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"


class BootstrapConfig(BaseModel):
    """The cluster bootstrap over customers (docs/22 §1 step 7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    replicates: int = Field(gt=0)
    level: float = Field(gt=0, lt=1)
    seed: int


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bootstrap: BootstrapConfig
    label_coverage_days: float = Field(gt=0)


def load_evaluation_config(path: Path = CONFIG_FILE) -> EvaluationConfig:
    with path.open(encoding="utf-8") as f:
        return EvaluationConfig.model_validate(yaml.safe_load(f))
