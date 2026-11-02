"""Engine value types (docs/06). Pure data: no I/O, no clocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

MEDIUM_TIER_RANK = 2


class Phase(StrEnum):
    """Which part of a loop is planning (docs/06 §9)."""

    GUARDIAN_PASSIVE = "GUARDIAN_PASSIVE"  # phase A: passive probes only, run without planning
    GUARDIAN_ACTIVE = "GUARDIAN_ACTIVE"  # phase B: active probes, planned, with exploration
    NAVIGATOR = "NAVIGATOR"


class Ineligible(StrEnum):
    """Why a source cannot be chosen now (docs/06 §8). Persisted in candidates tables."""

    ALREADY_OBSERVED = "ALREADY_OBSERVED"
    DISABLED = "DISABLED"
    WRONG_CONTEXT = "WRONG_CONTEXT"
    WRONG_CHANNEL = "WRONG_CHANNEL"
    ACTIVE_PROBE_IN_PASSIVE_PHASE = "ACTIVE_PROBE_IN_PASSIVE_PHASE"
    PASSIVE_PROBE_IN_ACTIVE_PHASE = "PASSIVE_PROBE_IN_ACTIVE_PHASE"
    NO_CONSENT = "NO_CONSENT"
    NO_LANGUAGE_VARIANT = "NO_LANGUAGE_VARIANT"
    FAIRNESS_CAP = "FAIRNESS_CAP"
    EXCLUSIVE_GROUP = "EXCLUSIVE_GROUP"
    ACTIVE_BUDGET = "ACTIVE_BUDGET"
    MEDIUM_BUDGET = "MEDIUM_BUDGET"


BUDGET_REASONS = frozenset({Ineligible.ACTIVE_BUDGET, Ineligible.MEDIUM_BUDGET})


class StopReason(StrEnum):
    NO_POSITIVE_VALUE = "NO_POSITIVE_VALUE"  # → terminal_reason STOP
    NO_ELIGIBLE_SOURCES = "NO_ELIGIBLE_SOURCES"  # → terminal_reason BUDGET_EXHAUSTED
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # → terminal_reason BUDGET_EXHAUSTED


class SelectedBy(StrEnum):
    GREEDY = "GREEDY"
    EXPLORE = "EXPLORE"


@dataclass(frozen=True, slots=True)
class CompiledSource:
    """A source compiled against one profile's hypothesis tuple."""

    key: str
    kind: str  # PROBE | EVIDENCE
    mode: str | None  # PASSIVE | ACTIVE | None (evidence)
    tier: str
    tier_rank: int
    outcomes: tuple[str, ...]
    likelihood: FloatArray  # shape (|O|, |H|); columns sum to 1
    cost_paise: float
    exclusive_group: str | None = None
    enabled: bool = True
    contexts: tuple[str, ...] = ()
    channels: tuple[str, ...] | None = None
    requires_consent: str | None = None
    question: bool = False

    @property
    def is_active(self) -> bool:
        return self.mode == "ACTIVE"

    @property
    def is_medium_plus(self) -> bool:
        return self.tier_rank >= MEDIUM_TIER_RANK


@dataclass(frozen=True)
class CompiledProfile:
    """A decision profile compiled to arrays (docs/06 §1). H always ends with UNEXPLAINED
    unless built for a fixture without it."""

    key: str
    context: str
    source_kind: str
    hypotheses: tuple[str, ...]
    decisions: tuple[str, ...]
    loss_base_paise: FloatArray  # (|D|, |H|)
    loss_rate: FloatArray  # (|D|, |H|)
    loss_vscaled: BoolArray  # (|D|, |H|)
    vulnerability_multiplier: float
    sources: dict[str, CompiledSource]
    lookahead_depth: int
    stop_epsilon_paise: float
    exploration_epsilon: float
    exploration_seed: int
    max_active_probes: int | None
    max_medium_plus: int | None
    open_mass_base: float = 0.02
    open_mass_slope: float = 0.18
    open_mass_shares: dict[str, float] = field(default_factory=dict)
    # derived in __post_init__ (vectorised planning, docs/06 §6 performance note)
    padded_likelihood: FloatArray = field(init=False, repr=False)  # (|S|, max|O|, |H|)
    source_index: dict[str, int] = field(init=False, repr=False)
    source_costs: FloatArray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        keys = list(self.sources)
        n_h = len(self.hypotheses)
        max_o = max((len(s.outcomes) for s in self.sources.values()), default=1)
        padded = np.zeros((len(keys), max_o, n_h), dtype=np.float64)
        for i, k in enumerate(keys):
            lik = self.sources[k].likelihood
            if lik.shape != (len(self.sources[k].outcomes), n_h):
                raise ValueError(f"likelihood of {k} has shape {lik.shape}")
            padded[i, : lik.shape[0], :] = lik
        padded.setflags(write=False)
        costs = np.array([self.sources[k].cost_paise for k in keys], dtype=np.float64)
        costs.setflags(write=False)
        object.__setattr__(self, "padded_likelihood", padded)
        object.__setattr__(self, "source_index", {k: i for i, k in enumerate(keys)})
        object.__setattr__(self, "source_costs", costs)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One row of the candidates table (docs/06 §15)."""

    source_key: str
    eligible: bool
    ineligible_reason: Ineligible | None
    cost_paise: float
    voi_myopic_paise: float | None
    net_value_paise: float | None
    settle_pct: float | None


@dataclass(frozen=True, slots=True)
class PlanResult:
    """Output of ``plan_next`` (docs/06 §15)."""

    action: str  # a source key or "STOP"
    stop_reason: StopReason | None
    selected_by: SelectedBy | None
    propensity: float | None
    bayes_risk_paise: float
    recommended_decision: str
    risk_by_decision: dict[str, float]
    candidates: tuple[Candidate, ...]
    lookahead_depth: int

    @property
    def is_stop(self) -> bool:
        return self.action == STOP


STOP = "STOP"
