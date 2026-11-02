"""DELPHI decision engine — the pure functional core (docs/06, ADR-0004, ADR-0005).

No I/O, no clocks, no global state. Imports only numpy and ``delphi.kb.model``.
"""

from delphi.engine.belief import as_belief, entropy_bits, kl_bits, predictive, settle_pct, update
from delphi.engine.compile import compile_profile, likelihood_matrix, make_source, with_sources
from delphi.engine.explain import FlipCandidate, top_hypotheses, what_would_change_my_mind
from delphi.engine.exploration import session_rng, session_seed
from delphi.engine.planner import (
    Budgets,
    SessionFacts,
    initial_budgets,
    plan_next,
    static_ineligibility,
    without_exploration,
)
from delphi.engine.prior import build_prior, is_fast_path, open_mass
from delphi.engine.risk import (
    bayes_risk,
    decide,
    expected_risk_after,
    loss_matrix,
    risk_by_decision,
    voi_myopic,
)
from delphi.engine.types import (
    STOP,
    Candidate,
    CompiledProfile,
    CompiledSource,
    Ineligible,
    Phase,
    PlanResult,
    SelectedBy,
    StopReason,
)

__all__ = [
    "STOP",
    "Budgets",
    "Candidate",
    "CompiledProfile",
    "CompiledSource",
    "FlipCandidate",
    "Ineligible",
    "Phase",
    "PlanResult",
    "SelectedBy",
    "SessionFacts",
    "StopReason",
    "as_belief",
    "bayes_risk",
    "build_prior",
    "compile_profile",
    "decide",
    "entropy_bits",
    "expected_risk_after",
    "initial_budgets",
    "is_fast_path",
    "kl_bits",
    "likelihood_matrix",
    "loss_matrix",
    "make_source",
    "open_mass",
    "plan_next",
    "predictive",
    "risk_by_decision",
    "session_rng",
    "session_seed",
    "settle_pct",
    "static_ineligibility",
    "top_hypotheses",
    "update",
    "voi_myopic",
    "what_would_change_my_mind",
    "with_sources",
    "without_exploration",
]
