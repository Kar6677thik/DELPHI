"""Depth-limited lookahead planner (docs/06 §6–10).

    V_0(p, S)     = R(p)
    Q_k(p, a, S)  = C(a) + Σ_o P(o | p, a) · V_{k−1}(p^{a,o}, S′(a))
    V_k(p, S)     = min(R(p), min_{a ∈ S} Q_k(p, a, S))
    net_value(a)  = R(p) − Q_k(p, a, S)

S′(a) re-applies every guardrail as if ``a`` had run (exclusive groups, active and MEDIUM+
budgets). Session-fixed guardrails (consent, language, fairness cap) are applied once up front.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from delphi.engine.belief import ZERO_PROBABILITY, settle_pct
from delphi.engine.risk import bayes_risk, loss_matrix, risk_by_decision, voi_myopic
from delphi.engine.types import (
    BUDGET_REASONS,
    STOP,
    Candidate,
    CompiledProfile,
    CompiledSource,
    FloatArray,
    Ineligible,
    Phase,
    PlanResult,
    SelectedBy,
    StopReason,
)

TIE_TOLERANCE_PAISE = 1.0  # docs/06 §6: values within 1 paise are ties
MEMO_DECIMALS = 12


@dataclass(frozen=True, slots=True)
class Budgets:
    """Per-session dynamic guardrail state (docs/06 §8). ``None`` means unlimited."""

    active_remaining: int | None = None
    medium_remaining: int | None = None
    used_groups: frozenset[str] = frozenset()

    def blocks(self, src: CompiledSource) -> Ineligible | None:
        if src.exclusive_group is not None and src.exclusive_group in self.used_groups:
            return Ineligible.EXCLUSIVE_GROUP
        if src.is_active and self.active_remaining is not None and self.active_remaining <= 0:
            return Ineligible.ACTIVE_BUDGET
        if src.is_medium_plus and self.medium_remaining is not None and self.medium_remaining <= 0:
            return Ineligible.MEDIUM_BUDGET
        return None

    def after(self, src: CompiledSource) -> Budgets:
        """The budgets as they would be once ``src`` has run."""
        active = self.active_remaining
        medium = self.medium_remaining
        if src.is_active and active is not None:
            active -= 1
        if src.is_medium_plus and medium is not None:
            medium -= 1
        groups = self.used_groups
        if src.exclusive_group is not None:
            groups = groups | {src.exclusive_group}
        return Budgets(active, medium, groups)


def initial_budgets(profile: CompiledProfile, already_run: tuple[str, ...] = ()) -> Budgets:
    """Budgets for a session given the sources that already ran (in any order)."""
    budgets = Budgets(profile.max_active_probes, profile.max_medium_plus)
    for key in already_run:
        budgets = budgets.after(profile.sources[key])
    return budgets


@dataclass(frozen=True, slots=True)
class SessionFacts:
    """Session-fixed facts for the static guardrails (docs/06 §8)."""

    channel: str | None = None
    consents: frozenset[str] = frozenset()
    servable_question_probes: frozenset[str] | None = None  # None: every question is servable
    fairness_cap_reached: bool = False


def static_ineligibility(
    profile: CompiledProfile,
    src: CompiledSource,
    facts: SessionFacts,
    observed: frozenset[str],
    phase: Phase,
) -> Ineligible | None:
    if src.key in observed:
        return Ineligible.ALREADY_OBSERVED
    if not src.enabled:
        return Ineligible.DISABLED
    if src.contexts and profile.context not in src.contexts:
        return Ineligible.WRONG_CONTEXT
    if src.channels is not None and facts.channel not in src.channels:
        return Ineligible.WRONG_CHANNEL
    if phase is Phase.GUARDIAN_PASSIVE and src.is_active:
        return Ineligible.ACTIVE_PROBE_IN_PASSIVE_PHASE
    if phase is Phase.GUARDIAN_ACTIVE and src.kind == "PROBE" and not src.is_active:
        return Ineligible.PASSIVE_PROBE_IN_ACTIVE_PHASE
    if src.requires_consent is not None and src.requires_consent not in facts.consents:
        return Ineligible.NO_CONSENT
    if (
        src.question
        and facts.servable_question_probes is not None
        and src.key not in facts.servable_question_probes
    ):
        return Ineligible.NO_LANGUAGE_VARIANT
    if src.is_active and facts.fairness_cap_reached:
        return Ineligible.FAIRNESS_CAP
    return None


class _ValueFunction:
    """Memoised V_k / Q_k for one planning call."""

    def __init__(self, profile: CompiledProfile, losses: FloatArray) -> None:
        self.profile = profile
        self.losses = losses
        self.memo: dict[tuple[bytes, frozenset[str], Budgets, int], float] = {}

    def value(self, p: FloatArray, keys: frozenset[str], budgets: Budgets, k: int) -> float:
        r = bayes_risk(p, self.losses)
        if k <= 0 or not keys:
            return r
        memo_key = (np.round(p, MEMO_DECIMALS).tobytes(), keys, budgets, k)
        cached = self.memo.get(memo_key)
        if cached is not None:
            return cached
        eligible = [kk for kk in keys if budgets.blocks(self.profile.sources[kk]) is None]
        if not eligible:
            result = r
        elif k == 1:
            result = min(r, float(np.min(self._q1_all(p, eligible))))
        else:
            best = r
            for kk in eligible:
                best = min(best, self.q(p, kk, keys, budgets, k))
            result = best
        self.memo[memo_key] = result
        return result

    def _q1_all(self, p: FloatArray, keys: list[str]) -> FloatArray:
        """Vectorised Q_1 for many sources: C(a) + Σ_o min_d Loss_d·(L_a[o]∘p)."""
        idx = [self.profile.source_index[k] for k in keys]
        joint = self.profile.padded_likelihood[idx] * p  # (A, O, H); padded rows are zero
        risks = np.einsum("dh,aoh->aod", self.losses, joint).min(axis=2).sum(axis=1)
        result: FloatArray = self.profile.source_costs[idx] + risks
        return result

    def q(self, p: FloatArray, key: str, keys: frozenset[str], budgets: Budgets, k: int) -> float:
        if k <= 1:
            return float(self._q1_all(p, [key])[0])
        src = self.profile.sources[key]
        joint = src.likelihood * p  # (O, H)
        probs = joint.sum(axis=1)
        rest = keys - {key}
        nxt = budgets.after(src)
        total = src.cost_paise
        for o in range(joint.shape[0]):
            if probs[o] > ZERO_PROBABILITY:
                total += probs[o] * self.value(joint[o] / probs[o], rest, nxt, k - 1)
        return total


def plan_next(
    profile: CompiledProfile,
    belief: FloatArray,
    *,
    amount_paise: float,
    vulnerable: bool,
    phase: Phase,
    observed: frozenset[str] = frozenset(),
    facts: SessionFacts | None = None,
    budgets: Budgets | None = None,
    rng: np.random.Generator | None = None,
) -> PlanResult:
    """Choose the next source to observe, or STOP (docs/06 §6, §10, §15)."""
    facts = facts or SessionFacts()
    budgets = budgets or Budgets()
    losses = loss_matrix(profile, amount_paise, vulnerable)
    risks = risk_by_decision(belief, losses)
    r = float(np.min(risks))
    recommended = profile.decisions[int(np.argmin(risks))]

    depth = profile.lookahead_depth
    if phase is Phase.GUARDIAN_ACTIVE and budgets.active_remaining is not None:
        depth = max(0, min(depth, budgets.active_remaining))

    reasons: dict[str, Ineligible | None] = {}
    for key, src in profile.sources.items():
        reason = static_ineligibility(profile, src, facts, observed, phase)
        reasons[key] = reason if reason is not None else budgets.blocks(src)
    statically_ok = frozenset(
        k
        for k, src in profile.sources.items()
        if static_ineligibility(profile, src, facts, observed, phase) is None
    )
    eligible = [k for k in profile.sources if reasons[k] is None]

    vf = _ValueFunction(profile, losses)
    net: dict[str, float] = {}
    if depth > 0:
        for key in eligible:
            net[key] = r - vf.q(belief, key, statically_ok, budgets, depth)

    def order_key(key: str) -> tuple[int, float, str]:
        src = profile.sources[key]
        return (src.tier_rank, src.cost_paise, key)

    candidates = _candidates(profile, belief, losses, reasons, net)
    risk_table = {d: float(v) for d, v in zip(profile.decisions, risks, strict=True)}

    def result(
        action: str,
        stop_reason: StopReason | None,
        selected_by: SelectedBy | None,
        propensity: float | None,
    ) -> PlanResult:
        return PlanResult(
            action=action,
            stop_reason=stop_reason,
            selected_by=selected_by,
            propensity=propensity,
            bayes_risk_paise=r,
            recommended_decision=recommended,
            risk_by_decision=risk_table,
            candidates=candidates,
            lookahead_depth=depth,
        )

    if not net:
        unobserved = [
            reasons[k] for k in profile.sources if reasons[k] is not Ineligible.ALREADY_OBSERVED
        ]
        stop = (
            StopReason.BUDGET_EXHAUSTED
            if any(x in BUDGET_REASONS for x in unobserved)
            else StopReason.NO_ELIGIBLE_SOURCES
        )
        return result(STOP, stop, None, None)

    best_value = max(net.values())
    if best_value <= profile.stop_epsilon_paise:
        return result(STOP, StopReason.NO_POSITIVE_VALUE, None, None)

    ties = [k for k, v in net.items() if v >= best_value - TIE_TOLERANCE_PAISE]
    greedy = min(ties, key=order_key)

    chosen, selected_by, propensity = greedy, SelectedBy.GREEDY, None
    if phase is Phase.GUARDIAN_ACTIVE and rng is not None and profile.exploration_epsilon > 0:
        chosen, selected_by, propensity = explore(profile, net, greedy, rng)
    return result(chosen, None, selected_by, propensity)


def explore(
    profile: CompiledProfile,
    net: dict[str, float],
    greedy: str,
    rng: np.random.Generator,
) -> tuple[str, SelectedBy, float]:
    """docs/06 §10: with prob ε pick uniformly from E = {net ≥ 0, tier ≤ tier(a*)}.

    Returns (chosen, selected_by, propensity). Consumes one draw, plus one more when exploring.
    """
    eps = profile.exploration_epsilon
    greedy_tier = profile.sources[greedy].tier_rank
    pool = sorted(
        k for k, v in net.items() if v >= 0 and profile.sources[k].tier_rank <= greedy_tier
    )
    explore = bool(rng.random() < eps)
    if explore:
        chosen = pool[int(rng.integers(len(pool)))]
        selected_by = SelectedBy.EXPLORE
    else:
        chosen = greedy
        selected_by = SelectedBy.GREEDY
    propensity = (1.0 - eps) * (1.0 if chosen == greedy else 0.0) + eps / len(pool)
    return chosen, selected_by, propensity


def _candidates(
    profile: CompiledProfile,
    belief: FloatArray,
    losses: FloatArray,
    reasons: dict[str, Ineligible | None],
    net: dict[str, float],
) -> tuple[Candidate, ...]:
    rows: list[Candidate] = []
    for key, src in profile.sources.items():
        reason = reasons[key]
        observed = reason is Ineligible.ALREADY_OBSERVED
        rows.append(
            Candidate(
                source_key=key,
                eligible=key in net,
                ineligible_reason=reason,
                cost_paise=src.cost_paise,
                voi_myopic_paise=None if observed else voi_myopic(belief, src.likelihood, losses),
                net_value_paise=net.get(key),
                settle_pct=None if observed else settle_pct(belief, src.likelihood),
            )
        )
    eligible = sorted(
        (c for c in rows if c.eligible),
        key=lambda c: (
            -(c.net_value_paise or 0.0),
            profile.sources[c.source_key].tier_rank,
            c.cost_paise,
            c.source_key,
        ),
    )
    others = sorted((c for c in rows if not c.eligible), key=lambda c: c.source_key)
    return (*eligible, *others)


def without_exploration(profile: CompiledProfile) -> CompiledProfile:
    """Copy of ``profile`` with ε = 0 (evaluation ablations, deterministic replays)."""
    return replace(profile, exploration_epsilon=0.0)
