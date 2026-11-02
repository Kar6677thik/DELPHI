"""Planner latency (NFR-03): p95 ≤ 50 ms for |S| ≤ 8, max|O| = 4, k = 3, |H| ≤ 8.

CI treats this as warn-level (docs/21 §1); the hard gate is the M9 load test. The assertion
allows 3× headroom for noisy shared CI runners and prints the measured figure.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from delphi.engine import CompiledProfile, Phase, SessionFacts, initial_budgets, plan_next, update
from tests.golden.test_story_behaviour import KAMALA_PASSIVE, KAMALA_PRIOR

TARGET_P95_MS = 50.0


@pytest.mark.benchmark
def test_planner_p95(guardian: CompiledProfile) -> None:
    p = np.asarray(KAMALA_PRIOR)
    for key, outcome in KAMALA_PASSIVE.items():
        src = guardian.sources[key]
        p = update(p, src.likelihood[src.outcomes.index(outcome)])
    facts = SessionFacts(consents=frozenset({"call_status"}))
    timings = []
    for _ in range(60):
        start = time.perf_counter()
        plan = plan_next(
            guardian,
            p,
            amount_paise=48_000_000,
            vulnerable=False,
            phase=Phase.GUARDIAN_ACTIVE,
            observed=frozenset(KAMALA_PASSIVE),
            facts=facts,
            budgets=initial_budgets(guardian),
        )
        timings.append((time.perf_counter() - start) * 1000)
    assert plan.lookahead_depth == 3
    p95 = float(np.percentile(timings[10:], 95))  # skip warm-up
    print(f"\nplanner p95 = {p95:.1f} ms (target {TARGET_P95_MS} ms)")
    assert p95 <= 3 * TARGET_P95_MS
