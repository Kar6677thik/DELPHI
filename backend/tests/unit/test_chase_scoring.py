"""Freeze-candidate scoring (docs/10 §4), on hand-computed fixtures."""

from __future__ import annotations

import math

import pytest

from delphi.chase.scoring import (
    US_PER_MINUTE,
    Candidate,
    Move,
    dwell_minutes,
    dwell_window_start,
    freeze_latency_minutes,
    score,
    times_to_out,
)
from delphi.kb.model import ChaseSection

# kb/engine.yaml `chase:` as of KB v1.
CHASE = ChaseSection.model_validate(
    {
        "min_trace_inr": 500,
        "freeze_latency_internal_s": 60,
        "freeze_latency_external_min": 30,
        "dwell_fallback_mule_min": 20,
        "dwell_fallback_other_h": 24,
        "lawful_hold_kappa": 0.30,
        "wrongful_base_inr": 5000,
        "wrongful_rate": 0.02,
        "max_liens_per_round": 10,
        "retrace_interval_min": 2,
        "ticket_expiry_min": 120,
        "visibility_lag_min": {"demo": 0, "eval": 5},
    }
)


def at(minutes: float) -> int:
    return round(minutes * US_PER_MINUTE)


def test_times_to_out_is_fifo_with_the_opening_balance_first() -> None:
    moves = [
        Move(at(-5), 700),  # before the window: not counted, but it leaves before later credits
        Move(at(0), 1000),
        Move(at(10), -2200),  # the opening 1000, the early 700, then 500 of the 1000: 50% out
        Move(at(30), -400),  # 100 of the 1000 left: 90% out, past 80%, at 30 min
        Move(at(40), 2000),  # never leaves: censored at now
    ]
    out = times_to_out(1000, moves, since_us=at(0), now_us=at(100))
    assert out == pytest.approx([30.0, 60.0])


def test_dwell_is_the_median_over_ln2_or_the_kb_fallback() -> None:
    assert dwell_minutes([10, 20, 60], 0.1, CHASE, 1.0) == pytest.approx(20 / math.log(2))
    assert dwell_minutes([10, 20, 60], 0.1, CHASE, 0.5) == pytest.approx(10 / math.log(2))
    # Fewer than 3 credits: 20 min for a likely mule, else 24 h, both time-scaled.
    assert dwell_minutes([5, 5], 0.5, CHASE, 1.0) == 20
    assert dwell_minutes([], 0.49, CHASE, 1.0) == 24 * 60
    assert dwell_minutes([], 0.9, CHASE, 0.1) == pytest.approx(2.0)


def test_latency_and_window() -> None:
    assert freeze_latency_minutes(True, CHASE, 1.0) == 1.0
    assert freeze_latency_minutes(False, CHASE, 0.1) == pytest.approx(3.0)
    assert dwell_window_start(at(30 * 24 * 60)) == 0


def test_scores_and_ranks_by_net_value() -> None:
    candidates = [
        # A likely mule at this bank, holding ₹80,000 of its ₹1,00,000 taint.
        Candidate("mule", 10_000_000, 8_000_000, 0.9, True, 20.0),
        # A probable innocent at another bank: the 30-min ticket, a 24 h dwell.
        Candidate("other", 3_000_000, 5_000_000, 0.1, False, 1440.0),
        # A clean holder of ₹600: the wrongful-lien cost outweighs what it could recover.
        Candidate("clean", 60_000, 60_000, 0.0, True, 1440.0),
        # Below the ₹500 trace floor: not a candidate at all.
        Candidate("dust", 40_000, 40_000, 0.9, True, 20.0),
    ]
    ranked = score(candidates, CHASE, 1.0)
    assert [s.account_id for s in ranked] == ["mule", "other", "clean"]

    mule, other, clean = ranked
    # lien 80,00,000 · e^(−1/20) · (0.9 + 0.1 · 0.3); wrongful 0.1 · (5,00,000 + 0.02 · lien).
    assert mule.lien_paise == 8_000_000
    assert mule.p_present == pytest.approx(math.exp(-1 / 20))
    assert (mule.erv_paise, mule.wrongful_paise, mule.net_paise) == (7_077_147, 66_000, 7_011_147)
    # lien 30,00,000 (the taint, below the balance) · e^(−30/1440) · (0.1 + 0.9 · 0.3).
    assert (other.erv_paise, other.wrongful_paise, other.net_paise) == (1_087_114, 504_000, 583_114)
    assert clean.net_paise < 0
    assert [s.rank for s in ranked] == [1, 2, 3]
    assert [s.recommended for s in ranked] == [True, True, False]


def test_the_lien_never_exceeds_the_balance() -> None:
    (s,) = score([Candidate("a", 1_000_000, 200_000, 1.0, True, 60.0)], CHASE, 1.0)
    assert s.lien_paise == 200_000
    assert s.wrongful_paise == 0  # a certain mule: no wrongful cost
    assert s.erv_paise == round(200_000 * math.exp(-1 / 60))
