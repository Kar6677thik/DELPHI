"""Hypothesis strategies: random but valid compiled profiles (docs/21 §2)."""

from __future__ import annotations

import numpy as np
from hypothesis import strategies as st

from delphi.engine import CompiledProfile, CompiledSource
from delphi.kb.model import TIER_RANK

TIERS = ("LOW", "MEDIUM", "HIGH")


@st.composite
def probability_vector(draw: st.DrawFn, size: int, floor: float = 1e-3) -> np.ndarray:
    raw = np.array(draw(st.lists(st.floats(0.0, 1.0), min_size=size, max_size=size)))
    vec = raw + floor * 10
    return vec / vec.sum()


@st.composite
def source(draw: st.DrawFn, key: str, n_h: int, *, active: bool | None = None) -> CompiledSource:
    n_o = draw(st.integers(2, 4))
    cols = [draw(probability_vector(n_o)) for _ in range(n_h)]
    lik = np.column_stack(cols)
    lik.setflags(write=False)
    is_active = draw(st.booleans()) if active is None else active
    tier = draw(st.sampled_from(TIERS)) if is_active else "NONE"
    return CompiledSource(
        key=key,
        kind="PROBE",
        mode="ACTIVE" if is_active else "PASSIVE",
        tier=tier,
        tier_rank=TIER_RANK[tier],
        outcomes=tuple(f"O{i}" for i in range(n_o)),
        likelihood=lik,
        cost_paise=draw(st.floats(0.0, 50_000.0)),
        exclusive_group=draw(st.sampled_from([None, None, "G1"])),
        contexts=("OUTBOUND_PAYMENT",),
    )


@st.composite
def profile(
    draw: st.DrawFn,
    *,
    max_h: int = 6,
    max_sources: int = 5,
    all_active: bool = False,
    max_active: int | None = 3,
) -> CompiledProfile:
    n_h = draw(st.integers(2, max_h))
    n_d = draw(st.integers(2, 4))
    n_s = draw(st.integers(1, max_sources))
    hyps = (*(f"H{i}" for i in range(n_h - 1)), "UNEXPLAINED")
    base = np.array(draw(st.lists(st.floats(0.0, 1e6), min_size=n_d * n_h, max_size=n_d * n_h)))
    rate = np.array(draw(st.lists(st.floats(0.0, 1.0), min_size=n_d * n_h, max_size=n_d * n_h)))
    vsc = np.array(draw(st.lists(st.booleans(), min_size=n_d * n_h, max_size=n_d * n_h)))
    sources = {
        f"S{i}": draw(source(f"S{i}", n_h, active=True if all_active else None)) for i in range(n_s)
    }
    return CompiledProfile(
        key="RANDOM",
        context="OUTBOUND_PAYMENT",
        source_kind="PROBE",
        hypotheses=hyps,
        decisions=tuple(f"D{i}" for i in range(n_d)),
        loss_base_paise=base.reshape(n_d, n_h),
        loss_rate=rate.reshape(n_d, n_h),
        loss_vscaled=vsc.reshape(n_d, n_h),
        vulnerability_multiplier=1.5,
        sources=sources,
        lookahead_depth=draw(st.integers(1, 3)),
        stop_epsilon_paise=100.0,
        exploration_epsilon=0.05,
        exploration_seed=1,
        max_active_probes=max_active,
        max_medium_plus=2,
        open_mass_shares={h: draw(st.floats(0.01, 0.9 / n_h)) for h in hyps[1:-1]},
    )


@st.composite
def belief_for(draw: st.DrawFn, n_h: int) -> np.ndarray:
    return draw(probability_vector(n_h, floor=1e-4))
