"""The learning module's facade for the api and the other feature modules (docs/04 §4.1 rule 3)."""

from __future__ import annotations

from delphi.learning.ope import (
    LoggedCandidate,
    LoggedProbe,
    OpeConfig,
    OpeEstimate,
    OpeInputError,
    OpeStatus,
    Policy,
    evaluate,
    first_choice,
    logged_probe,
    myopic_greedy,
    uniform,
)
from delphi.platform.settings import Settings

__all__ = [
    "LoggedCandidate",
    "LoggedProbe",
    "OpeConfig",
    "OpeEstimate",
    "OpeInputError",
    "OpeStatus",
    "Policy",
    "evaluate",
    "first_choice",
    "logged_probe",
    "myopic_greedy",
    "ope_config",
    "uniform",
]


def ope_config(settings: Settings) -> OpeConfig:
    return OpeConfig(
        bootstrap_resamples=settings.ope_bootstrap_resamples,
        min_ess=settings.ope_min_ess,
        min_support_coverage=settings.ope_min_support_coverage,
    )
