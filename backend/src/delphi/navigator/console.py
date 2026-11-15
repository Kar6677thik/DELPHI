"""The investigator console's decision support (docs/09 §2, docs/06 §6, §12–13, #103).

For a case's current posterior it computes what the console shows above the evidence cards:

1. the recommended decision with ``risk_by_decision`` (doc 06 §4);
2. **next best evidence**: every eligible, unobserved evidence item ranked by net value (the
   planner at the profile's depth, investigator-minute cost), with the expected minutes and the
   share of the remaining uncertainty it is expected to settle. In the COERCION protocol,
   ``CUSTOMER_OUTREACH_CALL`` is pinned first, with a "Protocol" badge, until it has been done;
3. **evidence sufficient**: the planner stops; the recommendation stands (fetching more is
   still allowed);
4. **what would change my mind** (doc 06 §12).

Pure: it takes the KB, the case's numbers and the evidence already observed, and reads nothing.
The LLM plays no part (ADR-0004).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from delphi.engine.compile import compile_profile
from delphi.engine.explain import FlipCandidate, what_would_change_my_mind
from delphi.engine.planner import plan_next
from delphi.engine.risk import loss_matrix
from delphi.engine.types import CompiledProfile, Phase
from delphi.kb.model import KnowledgeBase

OUTREACH = "CUSTOMER_OUTREACH_CALL"  # docs/09 §6: pinned first in the COERCION protocol
PROFILE_FOR_CASE_TYPE = {
    "PAYMENT_HOLD": "CASE_PAYMENT_HOLD",
    "ACCOUNT_REVIEW": "CASE_ACCOUNT_REVIEW",
}


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One row of "next best evidence" (docs/09 §2 item 2)."""

    source_key: str
    eligible: bool
    ineligible_reason: str | None
    net_value_paise: float | None
    cost_paise: float
    expected_minutes: float
    settle_pct: float | None
    pinned_by_protocol: bool = False


@dataclass(frozen=True, slots=True)
class ConsoleView:
    hypotheses: tuple[str, ...]
    posterior: Mapping[str, float]
    recommended_decision: str
    risk_by_decision: Mapping[str, float]
    next_best_evidence: tuple[EvidenceRow, ...]
    evidence_sufficient: bool
    what_would_change_my_mind: FlipCandidate | None


def case_profile(kb: KnowledgeBase, case_type: str) -> CompiledProfile:
    return compile_profile(kb, PROFILE_FOR_CASE_TYPE[case_type])


def belief_vector(profile: CompiledProfile, posterior: Mapping[str, float]) -> np.ndarray:
    """The stored posterior over the profile's hypotheses (missing ones are 0), renormalised."""
    belief = np.array([float(posterior.get(h, 0.0)) for h in profile.hypotheses])
    total = belief.sum()
    if total <= 0:
        raise ValueError("the case's posterior has no mass on the profile's hypotheses")
    normalised: np.ndarray = belief / total
    return normalised


def console(
    kb: KnowledgeBase,
    profile: CompiledProfile,
    *,
    posterior: Mapping[str, float],
    amount_paise: int,
    vulnerable: bool,
    protocol: str,
    observed: frozenset[str],
) -> ConsoleView:
    belief = belief_vector(profile, posterior)
    losses = loss_matrix(profile, float(amount_paise), vulnerable)
    plan = plan_next(
        profile,
        belief,
        amount_paise=float(amount_paise),
        vulnerable=vulnerable,
        phase=Phase.NAVIGATOR,
        observed=observed,
    )
    rows = [
        EvidenceRow(
            source_key=c.source_key,
            eligible=c.eligible,
            ineligible_reason=c.ineligible_reason.value if c.ineligible_reason else None,
            net_value_paise=c.net_value_paise,
            cost_paise=c.cost_paise,
            expected_minutes=float(kb.sources[c.source_key].expected_minutes or 0.0),
            settle_pct=c.settle_pct,
        )
        for c in plan.candidates  # eligible first, by net value (the planner's order)
        if c.source_key not in observed
    ]
    if protocol == "COERCION" and OUTREACH in profile.sources and OUTREACH not in observed:
        pinned = [r for r in rows if r.source_key == OUTREACH]
        rows = [_pin(r) for r in pinned] + [r for r in rows if r.source_key != OUTREACH]
    unobserved_eligible = [r.source_key for r in rows if r.eligible]
    return ConsoleView(
        hypotheses=profile.hypotheses,
        posterior=dict(zip(profile.hypotheses, (float(p) for p in belief), strict=True)),
        recommended_decision=plan.recommended_decision,
        risk_by_decision=dict(plan.risk_by_decision),
        next_best_evidence=tuple(rows),
        evidence_sufficient=plan.is_stop,
        what_would_change_my_mind=what_would_change_my_mind(
            profile, belief, losses, unobserved_eligible
        ),
    )


def _pin(row: EvidenceRow) -> EvidenceRow:
    return EvidenceRow(
        row.source_key,
        row.eligible,
        row.ineligible_reason,
        row.net_value_paise,
        row.cost_paise,
        row.expected_minutes,
        row.settle_pct,
        pinned_by_protocol=True,
    )
