"""The Guardian's session procedure as pure steps (docs/08 §2, docs/06; #123 plan §6).

The live ``GuardianService`` persists a session step by step (passive phase, planning, the
customer's answers, the decision), each in its own transaction. The evaluation harness replays
thousands of sessions in memory. Both run **these** functions, so the policy the harness scores
is the one the live Guardian runs:

- ``session_prior``: the prior from the sensing inputs (docs/06 §11), and ``takes_fast_path``;
- ``observe``: one observation's Bayes update (docs/06 §3);
- ``plan_step``: the next active probe or STOP (docs/06 §6–10), with the session's exploration
  stream for that step;
- ``stop_reason`` and ``final_decision``: how a session ends (docs/08 §2 step 9).

``run_session`` composes them into a whole session for a caller that has every answer to hand
(``answer`` returns the classified outcome of an active probe, or None for no answer in time).
It does no I/O and reads no clock; time-based guardrails (the session's maximum duration) are
the caller's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np

from delphi.engine.belief import update as bayes_update
from delphi.engine.planner import SessionFacts, initial_budgets, plan_next
from delphi.engine.prior import build_prior, is_fast_path, open_mass
from delphi.engine.risk import decide, loss_matrix, risk_by_decision
from delphi.engine.types import CompiledProfile, Phase, PlanResult, StopReason
from delphi.guardian.store import TerminalReason
from delphi.kb.model import UNEXPLAINED, KnowledgeBase
from delphi.sensing.api import PriorInputs, with_payer_mule

GENUINE = "GENUINE"


@dataclass(frozen=True, slots=True)
class SessionPrior:
    prior: np.ndarray
    p_model: dict[str, float]
    anomaly_percentile: float
    open_mass: float
    open_mass_shares: dict[str, float]


def session_prior(profile: CompiledProfile, inputs: PriorInputs) -> SessionPrior:
    """docs/06 §11: p_model with the payer's mule score, then the open-mass prior."""
    p_model = with_payer_mule(inputs.p_payment, inputs.p_mule_payer)
    q = float(inputs.anomaly_percentile)
    kb_only = [h for h in profile.hypotheses if h not in p_model and h != UNEXPLAINED]
    return SessionPrior(
        prior=build_prior(profile, p_model, q),
        p_model=dict(p_model),
        anomaly_percentile=q,
        open_mass=open_mass(q, profile.open_mass_base, profile.open_mass_slope),
        open_mass_shares={h: profile.open_mass_shares.get(h, 0.0) for h in kb_only},
    )


def takes_fast_path(kb: KnowledgeBase, prior: SessionPrior, amount_paise: float) -> bool:
    """docs/08 §3: release without probes when overwhelmingly genuine, small and usual."""
    fast = kb.config.engine.fast_path
    return is_fast_path(
        float(prior.p_model.get(GENUINE, 0.0)),
        float(amount_paise),
        prior.anomaly_percentile,
        min_p_genuine=fast.min_p_genuine,
        max_amount_inr=fast.max_amount_inr,
        max_anomaly_percentile=fast.max_anomaly_percentile,
    )


def observe(
    profile: CompiledProfile, belief: np.ndarray, source_key: str, outcome: str
) -> np.ndarray:
    """docs/06 §3: the belief after one observed outcome."""
    src = profile.sources[source_key]
    return bayes_update(belief, src.likelihood[src.outcomes.index(outcome)])


def plan_step(
    profile: CompiledProfile,
    belief: np.ndarray,
    *,
    amount_paise: float,
    vulnerable: bool,
    facts: SessionFacts,
    ran: tuple[str, ...],
    active: int,
    rng_seed: int,
    explore: bool,
) -> PlanResult:
    """docs/06 §6–10: the next active probe, or STOP. ``ran`` is every probe run so far (in
    order); ``active`` how many were active. One exploration stream per planning step, derived
    from the session's seed (docs/06 §10), so a resumed or replayed session draws the same
    numbers; ``explore=False`` plans greedily (ablations, replays)."""
    return plan_next(
        profile,
        belief,
        amount_paise=float(amount_paise),
        vulnerable=vulnerable,
        phase=Phase.GUARDIAN_ACTIVE,
        observed=frozenset(ran),
        facts=facts,
        budgets=initial_budgets(profile, ran),
        rng=np.random.default_rng([rng_seed, active]) if explore else None,
    )


def stop_reason(plan: PlanResult) -> TerminalReason:
    """The terminal reason of a STOP plan (docs/08 §2 step 8)."""
    if plan.stop_reason is StopReason.NO_POSITIVE_VALUE:
        return TerminalReason.STOP
    return TerminalReason.BUDGET_EXHAUSTED


def final_decision(
    profile: CompiledProfile, belief: np.ndarray, *, amount_paise: float, vulnerable: bool
) -> tuple[str, dict[str, float]]:
    """docs/08 §2 step 9: d*(posterior) and every decision's risk."""
    losses = loss_matrix(profile, float(amount_paise), vulnerable)
    idx, _ = decide(belief, losses)
    risks = risk_by_decision(belief, losses)
    return profile.decisions[idx], {
        d: float(r) for d, r in zip(profile.decisions, risks, strict=True)
    }


# ------------------------------------------------------------------ a whole session


@dataclass(frozen=True, slots=True)
class ProbeStep:
    probe_key: str
    active: bool
    outcome: str | None  # None: no outcome (a passive error, or no answer in time)
    belief_before: np.ndarray
    belief_after: np.ndarray
    plan: PlanResult | None = None  # the plan that chose an active probe


@dataclass(frozen=True, slots=True)
class SessionResult:
    prior: SessionPrior
    decision: str
    terminal_reason: TerminalReason
    posterior: np.ndarray
    risk_by_decision: dict[str, float]
    steps: tuple[ProbeStep, ...]


def run_session(
    profile: CompiledProfile,
    kb: KnowledgeBase,
    inputs: PriorInputs,
    *,
    amount_paise: float,
    vulnerable: bool,
    facts: SessionFacts,
    passive: Mapping[str, str | None],
    answer: Callable[[str], str | None],
    rng_seed: int,
    explore: bool = True,
) -> SessionResult:
    """docs/08 §2 steps 2–9 for a caller with every answer to hand: the prior (and the fast
    path), the passive outcomes in KB source order (None: the adapter failed, no update), then
    plan, ask and update until STOP, then decide."""
    prior = session_prior(profile, inputs)
    belief = prior.prior
    steps: list[ProbeStep] = []

    def end(reason: TerminalReason) -> SessionResult:
        decision, risks = final_decision(
            profile, belief, amount_paise=amount_paise, vulnerable=vulnerable
        )
        return SessionResult(prior, decision, reason, belief, risks, tuple(steps))

    if takes_fast_path(kb, prior, amount_paise):
        return end(TerminalReason.FAST_PATH)
    for key in (k for k in profile.sources if k in passive):
        outcome = passive[key]
        after = belief if outcome is None else observe(profile, belief, key, outcome)
        steps.append(ProbeStep(key, False, outcome, belief, after))
        belief = after
    active = 0
    while True:
        plan = plan_step(
            profile,
            belief,
            amount_paise=amount_paise,
            vulnerable=vulnerable,
            facts=facts,
            ran=tuple(s.probe_key for s in steps),
            active=active,
            rng_seed=rng_seed,
            explore=explore,
        )
        if plan.is_stop:
            return end(stop_reason(plan))
        outcome = answer(plan.action)
        after = belief if outcome is None else observe(profile, belief, plan.action, outcome)
        steps.append(ProbeStep(plan.action, True, outcome, belief, after, plan))
        belief = after
        active += 1
