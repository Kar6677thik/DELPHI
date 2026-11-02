"""Golden test: research §9.3 worked example, exactly as in docs/06 §16."""

from __future__ import annotations

import numpy as np
import pytest
import yaml

from delphi.engine import (
    CompiledProfile,
    bayes_risk,
    compile_profile,
    decide,
    loss_matrix,
    make_source,
    update,
    voi_myopic,
    with_sources,
)
from delphi.kb.model import KnowledgeBase
from tests.conftest import FIXTURES

PROB_TOL = 1e-3
MONEY_TOL_PAISE = 10.0  # ±₹0.10


@pytest.fixture(scope="module")
def example(kb: KnowledgeBase) -> dict:
    with (FIXTURES / "kb_research_example.yaml").open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    base = compile_profile(
        kb, "GUARDIAN_PAYMENT", hypotheses=data["hypotheses"], include_unexplained=False
    )
    sources = {
        key: make_source(key, spec["outcomes"], base.hypotheses, spec["likelihoods"])
        for key, spec in data["sources"].items()
    }
    profile = with_sources(base, sources)
    prior = np.array([data["prior"][h] for h in profile.hypotheses])
    losses = loss_matrix(profile, data["amount_inr"] * 100, data["vulnerable"])
    return {"profile": profile, "prior": prior, "losses": losses}


def _posterior(profile: CompiledProfile, belief: np.ndarray) -> dict[str, float]:
    return {h: float(p) for h, p in zip(profile.hypotheses, belief, strict=True)}


def _observe(profile: CompiledProfile, belief: np.ndarray, key: str, outcome: str) -> np.ndarray:
    src = profile.sources[key]
    return update(belief, src.likelihood[src.outcomes.index(outcome)])


def test_loss_table_matches_spec(example: dict) -> None:
    profile, losses = example["profile"], example["losses"]
    expected_inr = {  # docs/06 §16 loss table, A = ₹4,80,000
        "RELEASE": {"GENUINE": 0, "APP_DIGITAL_ARREST": 456_000, "ATO": 432_000},
        "HOLD_FOR_REVIEW": {"GENUINE": 15_400, "APP_DIGITAL_ARREST": 38_600, "ATO": 5_000},
        "PROTECTIVE_HOLD": {"GENUINE": 22_200, "APP_DIGITAL_ARREST": 5_000, "ATO": 5_100},
        "DECLINE": {"GENUINE": 26_000, "APP_DIGITAL_ARREST": 96_050, "ATO": 50},
    }
    for d, row in expected_inr.items():
        for h, inr in row.items():
            got = losses[profile.decisions.index(d), profile.hypotheses.index(h)]
            assert got == pytest.approx(inr * 100, abs=1e-6), (d, h)


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        (
            [("OTP_STEP_UP", "PASSED")],
            {"GENUINE": 0.593, "APP_DIGITAL_ARREST": 0.374, "ATO": 0.033},
        ),
        (
            [("CALL_STATUS_BINARY", "ON_CALL_UNKNOWN_NUMBER")],
            {"GENUINE": 0.125, "APP_DIGITAL_ARREST": 0.846, "ATO": 0.028},
        ),
        (
            [
                ("CALL_STATUS_BINARY", "ON_CALL_UNKNOWN_NUMBER"),
                ("SPELL_BREAKER_DIGITAL_ARREST_BINARY", "HESITATION_OR_APP_SWITCH"),
            ],
            {"GENUINE": 0.012, "APP_DIGITAL_ARREST": 0.977, "ATO": 0.011},
        ),
    ],
)
def test_posteriors(
    example: dict, steps: list[tuple[str, str]], expected: dict[str, float]
) -> None:
    belief = example["prior"]
    for key, outcome in steps:
        belief = _observe(example["profile"], belief, key, outcome)
    got = _posterior(example["profile"], belief)
    for h, p in expected.items():
        assert got[h] == pytest.approx(p, abs=PROB_TOL), h


def test_prior_risk_and_decision(example: dict) -> None:
    idx, risk = decide(example["prior"], example["losses"])
    assert example["profile"].decisions[idx] == "PROTECTIVE_HOLD"
    assert risk == pytest.approx(14_470.00 * 100, abs=MONEY_TOL_PAISE)
    assert bayes_risk(example["prior"], example["losses"]) == pytest.approx(risk)


def test_otp_has_zero_value_against_coercion(example: dict) -> None:
    """The slide claim: DELPHI knows before running it that OTP is worthless here."""
    src = example["profile"].sources["OTP_STEP_UP"]
    assert voi_myopic(example["prior"], src.likelihood, example["losses"]) == pytest.approx(
        0.0, abs=MONEY_TOL_PAISE
    )


def test_call_status_value(example: dict) -> None:
    src = example["profile"].sources["CALL_STATUS_BINARY"]
    assert voi_myopic(example["prior"], src.likelihood, example["losses"]) == pytest.approx(
        1_685.80 * 100, abs=MONEY_TOL_PAISE
    )
