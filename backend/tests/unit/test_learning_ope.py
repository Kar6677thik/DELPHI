"""docs/12 §3 / FR-LRN-03: SNIPS for the first active probe, against hand-computed values."""

from __future__ import annotations

import math
from uuid import UUID

import numpy as np
import pytest

from delphi.engine.planner import TIE_TOLERANCE_PAISE, explore
from delphi.engine.types import CompiledProfile
from delphi.learning.ope import (
    LoggedCandidate,
    LoggedProbe,
    OpeConfig,
    OpeInputError,
    OpeStatus,
    RebuildMismatchError,
    RoundRow,
    collect_rounds,
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
        "status": "COMPLETED",
        "selected_by": "EXPLORE",
        "propensity": 0.1,  # ε / |E| = 0.2 / 2
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
    tiers = {"X": 1, "Y": 1}
    probe = logged_probe(UUID(int=9), run, "APP_INVESTMENT", tiers, EPS)
    assert probe.exploration_set == ("X", "Y")
    assert probe.reward == pytest.approx(math.log2(0.8 / 0.5))
    with pytest.raises(OpeInputError):
        logged_probe(UUID(int=9), {**run, "propensity": None}, "GENUINE", tiers, EPS)
    # #210 review: a run with no posterior after it (an ERROR run) is an input error, not a
    # TypeError, and only COMPLETED and TIMED_OUT runs count (docs/12 §3).
    with pytest.raises(OpeInputError, match="no posterior"):
        logged_probe(UUID(int=9), {**run, "posterior_after": None}, "GENUINE", tiers, EPS)
    with pytest.raises(OpeInputError, match="ERROR"):
        logged_probe(UUID(int=9), {**run, "status": "ERROR"}, "GENUINE", tiers, EPS)
    timed_out = {**run, "status": "TIMED_OUT", "posterior_after": run["posterior_before"]}
    assert logged_probe(UUID(int=9), timed_out, "GENUINE", tiers, EPS).reward == 0.0


def _row(
    nets: dict[str, float],
    tiers: dict[str, int],
    action: str,
    selected_by: str,
    propensity: float,
    *,
    session: int = 1,
) -> RoundRow:
    """A probe run as Guardian stores it: each net rounded to the paise (``_paise``)."""
    run = {
        "probe_key": action,
        "status": "COMPLETED",
        "selected_by": selected_by,
        "propensity": propensity,
        "candidates": [
            {
                "source_key": key,
                "eligible": True,
                "cost_paise": 100,
                "voi_myopic_paise": 0,
                "net_value_paise": round(net),
            }
            for key, net in nets.items()
        ],
        "posterior_before": {"GENUINE": 0.5, "MULE": 0.5},
        "posterior_after": {"GENUINE": 0.6, "MULE": 0.4},
    }
    return RoundRow(UUID(int=session), run, "GENUINE", tiers, EPS)


def test_a_negative_net_stored_as_zero_is_a_rebuild_mismatch() -> None:
    # The planner's E is {X}: Y's net of −0.4 paise is below 0. Stored, it reads 0, so the
    # rebuilt E would be {X, Y} and the logged propensity 1 would be taken for 0.8 + 0.2 / 2.
    bad = _row({"X": 500.0, "Y": -0.4}, {"X": 1, "Y": 1}, "X", "GREEDY", 1.0, session=1)
    good = _row({"X": 500.0, "Y": 3.0}, {"X": 1, "Y": 1}, "X", "GREEDY", 0.8 + EPS / 2, session=2)
    with pytest.raises(RebuildMismatchError):
        logged_probe(bad.session_id, bad.run, bad.label, bad.tier_rank, bad.epsilon)
    rounds = collect_rounds([bad, good])
    assert [p.session_id for p in rounds.probes] == [UUID(int=2)]
    assert rounds.rebuild_mismatch == 1


def test_a_tie_made_by_rounding_is_a_rebuild_mismatch() -> None:
    # 100.6 and 99.5 are not a tie as floats (a* = A, tier 2). Rounded to 101 and 100 they are,
    # and the tie-break picks B (tier 1): the rebuilt a* is not the logged greedy action.
    row = _row({"A": 100.6, "B": 99.5}, {"A": 2, "B": 1}, "A", "GREEDY", 0.8 + EPS / 2)
    rounds = collect_rounds([row])
    assert rounds.probes == () and rounds.rebuild_mismatch == 1


def test_the_rebuild_reproduces_the_engines_explore(guardian: CompiledProfile) -> None:
    """The engine's own `explore()`, on nets the rounding leaves exact: `logged_probe`
    rebuilds its pool, a* and propensity, for greedy and explored rounds alike."""
    rng = np.random.default_rng(210)
    tiers = {k: s.tier_rank for k, s in guardian.sources.items()}
    eps = guardian.exploration_epsilon
    assert eps > 0
    kinds = set()
    for session in range(200):
        keys = rng.choice(sorted(guardian.sources), size=int(rng.integers(2, 6)), replace=False)
        net = {str(k): float(rng.integers(-300, 3_000)) for k in keys}
        if max(net.values()) < 0:
            continue

        def order(key: str) -> tuple[int, float, str]:
            src = guardian.sources[key]
            return (src.tier_rank, src.cost_paise, key)

        best = max(net.values())
        greedy = min((k for k, v in net.items() if v >= best - TIE_TOLERANCE_PAISE), key=order)
        chosen, selected_by, propensity = explore(guardian, net, greedy, rng)
        kinds.add(selected_by.value)
        run = _row(net, {}, chosen, selected_by.value, propensity).run
        for c in run["candidates"]:
            c["cost_paise"] = round(guardian.sources[c["source_key"]].cost_paise)
        probe = logged_probe(UUID(int=session), run, "GENUINE", tiers, eps)
        pool = sorted(k for k, v in net.items() if v >= 0 and tiers[k] <= tiers[greedy])
        assert probe.exploration_set == tuple(pool)
        assert probe.propensity == propensity
    assert kinds == {"GREEDY", "EXPLORE"}


def test_true_gain_needs_the_label_in_the_posterior() -> None:
    assert true_gain({"G": 0.25}, {"G": 0.5}, "G") == pytest.approx(1.0)
    with pytest.raises(OpeInputError):
        true_gain({"G": 0.25}, {"G": 0.5}, "MULE")


def test_an_action_without_propensity_is_rejected() -> None:
    bad = LoggedProbe(UUID(int=4), "Z", 0.0, ("X", "Y"), CANDS, reward=0.0)
    with pytest.raises(OpeInputError):
        weights((bad,), uniform)
