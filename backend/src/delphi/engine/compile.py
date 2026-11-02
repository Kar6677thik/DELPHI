"""Compile a KB decision profile into arrays the engine can compute with (docs/06 §1–2, §7)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

import numpy as np

from delphi.engine.types import CompiledProfile, CompiledSource, FloatArray
from delphi.kb.model import GENUINE, TIER_RANK, UNEXPLAINED, KnowledgeBase, ObservationSource


def _source_cost_paise(kb: KnowledgeBase, src: ObservationSource) -> float:
    """docs/06 §7: Guardian friction + delay; Navigator investigator minutes."""
    costs = kb.config.costs
    if src.kind == "PROBE":
        latency = src.expected_latency_s or 0.0
        return (
            costs.friction_cost_inr[src.friction_tier] + latency * costs.delay_cost_inr_per_s
        ) * 100.0
    return (src.expected_minutes or 0.0) * costs.investigator_cost_inr_per_min * 100.0


def likelihood_matrix(
    outcomes: Sequence[str],
    hypotheses: Sequence[str],
    rows: Mapping[str, Sequence[float]],
) -> FloatArray:
    """Build P(o | h) as (|O|, |H|). UNEXPLAINED and missing rows are uniform (docs/06 §2)."""
    n_o = len(outcomes)
    mat = np.empty((n_o, len(hypotheses)), dtype=np.float64)
    for j, h in enumerate(hypotheses):
        row = rows.get(h) if h != UNEXPLAINED else None
        if row is None:
            mat[:, j] = 1.0 / n_o
        else:
            vec = np.asarray(row, dtype=np.float64)
            if vec.shape != (n_o,):
                raise ValueError(f"row for {h} has {vec.shape[0]} entries, expected {n_o}")
            mat[:, j] = vec / vec.sum()  # posterior-mean of the Dirichlet: alpha / sum(alpha)
    mat.setflags(write=False)
    return mat


def make_source(
    key: str,
    outcomes: Sequence[str],
    hypotheses: Sequence[str],
    rows: Mapping[str, Sequence[float]],
    *,
    kind: str = "PROBE",
    mode: str | None = "ACTIVE",
    tier: str = "LOW",
    cost_paise: float = 0.0,
    exclusive_group: str | None = None,
    contexts: Sequence[str] = ("OUTBOUND_PAYMENT",),
) -> CompiledSource:
    """Build a compiled source directly (fixtures, backtests)."""
    return CompiledSource(
        key=key,
        kind=kind,
        mode=mode,
        tier=tier,
        tier_rank=TIER_RANK[tier],
        outcomes=tuple(outcomes),
        likelihood=likelihood_matrix(outcomes, hypotheses, rows),
        cost_paise=cost_paise,
        exclusive_group=exclusive_group,
        contexts=tuple(contexts),
    )


def compile_profile(
    kb: KnowledgeBase,
    profile_key: str,
    *,
    hypotheses: Sequence[str] | None = None,
    include_unexplained: bool = True,
) -> CompiledProfile:
    """Compile ``profile_key``. ``hypotheses`` restricts H (KB order kept) for fixtures."""
    profile = kb.decision_profiles[profile_key]
    applicable = [h for h in kb.hypothesis_order if profile.context in kb.hypotheses[h].contexts]
    if hypotheses is not None:
        unknown = set(hypotheses) - set(applicable)
        if unknown:
            raise ValueError(f"hypotheses not applicable to {profile_key}: {sorted(unknown)}")
        applicable = [h for h in applicable if h in set(hypotheses)]
    if GENUINE not in applicable:
        raise ValueError("GENUINE must be among the hypotheses")
    hyps = (*applicable, UNEXPLAINED) if include_unexplained else tuple(applicable)

    n_d, n_h = len(profile.decisions), len(hyps)
    base = np.zeros((n_d, n_h))
    rate = np.zeros((n_d, n_h))
    vscaled = np.zeros((n_d, n_h), dtype=np.bool_)
    for j, h in enumerate(hyps):
        cells = (
            profile.loss[h] if h in (GENUINE, UNEXPLAINED) else kb.hypotheses[h].loss[profile.key]
        )
        for i, d in enumerate(profile.decisions):
            cell = cells[d]
            base[i, j] = cell.base_inr * 100.0
            rate[i, j] = cell.rate
            vscaled[i, j] = cell.vulnerability_scaled
    for arr in (base, rate, vscaled):
        arr.setflags(write=False)

    sources: dict[str, CompiledSource] = {}
    for src in kb.ordered_sources():
        if src.kind != profile.source_kind:
            continue
        rows = {
            h: kb.hypotheses[h].likelihoods[src.key].alpha
            for h in applicable
            if src.key in kb.hypotheses[h].likelihoods
        }
        sources[src.key] = CompiledSource(
            key=src.key,
            kind=src.kind,
            mode=src.mode,
            tier=src.friction_tier,
            tier_rank=TIER_RANK[src.friction_tier],
            outcomes=src.outcomes,
            likelihood=likelihood_matrix(src.outcomes, hyps, rows),
            cost_paise=_source_cost_paise(kb, src),
            exclusive_group=src.exclusive_group,
            enabled=src.enabled,
            contexts=src.contexts,
            channels=src.channels,
            requires_consent=src.requires_consent,
            question=src.question,
        )

    cfg = kb.config
    guardian = profile.source_kind == "PROBE"
    return CompiledProfile(
        key=profile.key,
        context=profile.context,
        source_kind=profile.source_kind,
        hypotheses=hyps,
        decisions=profile.decisions,
        loss_base_paise=base,
        loss_rate=rate,
        loss_vscaled=vscaled,
        vulnerability_multiplier=cfg.engine.vulnerability_multiplier,
        sources=sources,
        lookahead_depth=(
            cfg.engine.lookahead_depth.guardian
            if guardian
            else cfg.engine.lookahead_depth.navigator
        ),
        stop_epsilon_paise=cfg.engine.stop_epsilon_paise,
        exploration_epsilon=cfg.engine.exploration_epsilon if guardian else 0.0,
        exploration_seed=cfg.engine.exploration_seed,
        max_active_probes=cfg.guardrails.max_active_probes_per_session if guardian else None,
        max_medium_plus=cfg.guardrails.max_medium_plus_per_session if guardian else None,
        open_mass_base=cfg.engine.open_mass_base,
        open_mass_slope=cfg.engine.open_mass_slope,
        open_mass_shares={
            h: kb.hypotheses[h].open_mass_share or 0.0 for h in applicable if h != GENUINE
        },
    )


def with_sources(
    profile: CompiledProfile, sources: Mapping[str, CompiledSource]
) -> CompiledProfile:
    """Return a copy of ``profile`` whose sources are replaced (fixtures, backtests)."""
    return replace(profile, sources=dict(sources))
