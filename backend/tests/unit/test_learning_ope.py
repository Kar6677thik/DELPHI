"""docs/12 §3 / FR-LRN-03: SNIPS for the first active probe, against hand-computed values."""

from __future__ import annotations

import math
from uuid import UUID

import numpy as np
import pytest

from delphi.learning.ope import (
    LoggedCandidate,
    LoggedProbe,
    OpeConfig,
    OpeInputError,
    OpeStatus,
    effective_sample_size,
    evaluate,
    exploration_set,
    first_choice,
    logged_probe,
    myopic_greedy,
    true_gain,
    uniform,
    weights,
)

LOOSE = OpeConfig(bootstrap_resamples=1_000, min_ess=1.0, min_support_coverage=0.5)
EPS = 0.2  # the logging policy's ε in the example


def _cand(
    key: str, *, tier: int = 1, cost: int = 100, voi: int = 0, net: int = 0
) -> LoggedCandidate:
    return LoggedCandidate(key, True, tier, cost, voi, net)


# Three labelled sessions, logged with ε = 0.2 (docs/06 §10: π0(a) = 0.8·[a = a*] + 0.2 / |E|).
# Greedy a* is X in each. Candidates: X has the best net (lookahead) value, Y the best myopic.
CANDS = (
    _cand("X", voi=300, net=500),
    _cand("Y", voi=900, net=400),
    _cand("Z", voi=100, net=300),
)
A = LoggedProbe(UUID(int=1), "X", 0.8 + EPS / 2, ("X", "Y"), CANDS, reward=1.0)
B = LoggedProbe(UUID(int=2), "Y", EPS / 2, ("X", "Y"), CANDS, reward=2.0)
C = LoggedProbe(UUID(int=3), "X", 0.8 + EPS / 3, ("X", "Y", "Z"), CANDS, reward=0.5)
LOGGED = (A, B, C)


def test_uniform_target_by_hand() -> None:
    w, gap = weights(LOGGED, uniform)
    # w = π/π0: (1/2)/0.9, (1/2)/0.1, (1/3)/(0.8 + 0.2/3)
    assert gap == 0
    assert w == pytest.approx([5 / 9, 5.0, 5 / 13])
    est = evaluate("uniform", LOGGED, uniform, LOOSE, np.random.default_rng(0))
    value = (5 / 9 * 1.0 + 5.0 * 2.0 + 5 / 13 * 0.5) / (5 / 9 + 5.0 + 5 / 13)
    ess = (5 / 9 + 5.0 + 5 / 13) ** 2 / ((5 / 9) ** 2 + 25.0 + (5 / 13) ** 2)
    assert est.status is OpeStatus.OK
    assert est.value == pytest.approx(value)  # 1.7933…
    assert est.ess == pytest.approx(ess)
    assert est.support_coverage == 1.0
    assert est.ci_low is not None and est.ci_high is not None
    assert min(r.reward for r in LOGGED) <= est.ci_low <= est.value <= est.ci_high <= 2.0


def test_deterministic_target_by_hand() -> None:
    # Myopic greedy chooses Y everywhere: only session B logged Y, with w = 1 / 0.1.
    w, gap = weights(LOGGED, myopic_greedy)
    assert gap == 0 and w == pytest.approx([0.0, 10.0, 0.0])
    est = evaluate("myopic", LOGGED, myopic_greedy, LOOSE, np.random.default_rng(0))
    assert est.value == pytest.approx(2.0)
    assert est.ess == pytest.approx(1.0)


def test_logged_policy_is_the_on_policy_mean() -> None:
    est = evaluate("logged", LOGGED, None, LOOSE, np.random.default_rng(0))
    assert est.value == pytest.approx((1.0 + 2.0 + 0.5) / 3)
    assert est.ess == pytest.approx(3.0)


def test_a_target_outside_e_has_no_support() -> None:
    # "Z-first": Z is eligible everywhere but in E only in session C, where X was logged.
    w, gap = weights(LOGGED, first_choice("Z"))
    assert gap == 2 and w == pytest.approx([0.0, 0.0, 0.0])
    est = evaluate("Z-first", LOGGED, first_choice("Z"), LOOSE, np.random.default_rng(0))
    assert est.status is OpeStatus.UNSUPPORTED and est.value is None
    assert est.support_coverage == pytest.approx(1 / 3)


def test_first_choice_falls_back_to_greedy_when_not_eligible() -> None:
    w, gap = weights(LOGGED, first_choice("OTP_STEP_UP"))  # not a candidate: greedy X
    assert gap == 0
    assert w == pytest.approx([1 / 0.9, 0.0, 1 / (0.8 + EPS / 3)])


def test_low_ess_is_insufficient_data() -> None:
    strict = OpeConfig(bootstrap_resamples=1_000, min_ess=30.0, min_support_coverage=0.5)
    est = evaluate("uniform", LOGGED, uniform, strict, np.random.default_rng(0))
    assert est.status is OpeStatus.INSUFFICIENT_DATA and est.value is None
    assert est.ess < 30


def test_effective_sample_size_of_equal_weights_is_n() -> None:
    assert effective_sample_size(np.full(7, 0.4)) == pytest.approx(7.0)
    assert effective_sample_size(np.zeros(3)) == 0.0


def test_the_bootstrap_is_reproducible() -> None:
    a = evaluate("uniform", LOGGED * 20, uniform, LOOSE, np.random.default_rng(5))
    b = evaluate("uniform", LOGGED * 20, uniform, LOOSE, np.random.default_rng(5))
    assert (a.ci_low, a.ci_high) == (b.ci_low, b.ci_high)


def test_exploration_set_follows_docs_06_section_10() -> None:
    cands = (
        _cand("A", tier=1, net=500),  # a*
        _cand("B", tier=1, net=-5),  # net < 0: out
        _cand("C", tier=2, net=50),  # a higher tier than a*: out
        _cand("D", tier=0, net=0),
        LoggedCandidate("E", False, 0, 10, 0, 900),  # ineligible: out
    )
    assert exploration_set(cands) == ("A", "D")


def test_logged_probe_from_a_probe_run_row() -> None:
    run = {
        "probe_key": "Y",
        "propensity": 0.1,
        "candidates": [
            {
                "source_key": "X",
                "eligible": True,
                "cost_paise": 100,
                "voi_myopic_paise": 300,
                "net_value_paise": 500,
            },
            {
                "source_key": "Y",
                "eligible": True,
                "cost_paise": 100,
                "voi_myopic_paise": 900,
                "net_value_paise": 400,
            },
        ],
        "posterior_before": {"GENUINE": 0.5, "APP_INVESTMENT": 0.5},
        "posterior_after": {"GENUINE": 0.2, "APP_INVESTMENT": 0.8},
    }
    probe = logged_probe(UUID(int=9), run, "APP_INVESTMENT", {"X": 1, "Y": 1})
    assert probe.exploration_set == ("X", "Y")
    assert probe.reward == pytest.approx(math.log2(0.8 / 0.5))
    with pytest.raises(OpeInputError):
        logged_probe(UUID(int=9), {**run, "propensity": None}, "GENUINE", {"X": 1, "Y": 1})


def test_true_gain_needs_the_label_in_the_posterior() -> None:
    assert true_gain({"G": 0.25}, {"G": 0.5}, "G") == pytest.approx(1.0)
    with pytest.raises(OpeInputError):
        true_gain({"G": 0.25}, {"G": 0.5}, "MULE")


def test_an_action_without_propensity_is_rejected() -> None:
    bad = LoggedProbe(UUID(int=4), "Z", 0.0, ("X", "Y"), CANDS, reward=0.0)
    with pytest.raises(OpeInputError):
        weights((bad,), uniform)
