"""Off-policy evaluation of the first active probe (docs/12 §3, FR-LRN-03).

Each Guardian session's first active probe is a contextual-bandit round: the context is the
session state just before it, the action the probe the planner chose with its logged
propensity π0(a | x), and the reward the true information gain of that probe,
``log2 p'(h_true) − log2 p(h_true)``, for labelled sessions only.

SNIPS re-weights the logged rewards by ``w_i = π(a_i | x_i) / π0(a_i | x_i)`` to estimate what a
target policy π would have gained. π0 is positive only on the logged exploration set E_i (docs/06
§10), so a deterministic target that chooses outside E_i has no support in that session: it gets
``w_i = 0`` and counts in ``support_gap``.

Pure: numpy only, no I/O. The rows come from ``probe_runs`` and the labels; building them is the
caller's job (``logged_probe``).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import numpy as np

# docs/06 §6: net values within 1 paise are ties, broken by (tier, cost, key).
TIE_TOLERANCE_PAISE = 1
# docs/12 §3: the CI is a 95% percentile bootstrap over sessions.
CI_LEVEL = 0.95
# A logged propensity and the one rebuilt from the row agree to float rounding, or not at all.
PROPENSITY_TOLERANCE = 1e-9
# docs/12 §3: the probe-run statuses whose round counts. A TIMED_OUT run stores the belief
# unchanged, so its reward is 0; PENDING and AWAITING_CUSTOMER haven't ended, and an ERROR run
# (a cancelled session, say) observed nothing.
COUNTED_STATUSES = frozenset({"COMPLETED", "TIMED_OUT"})


class OpeStatus(StrEnum):
    OK = "OK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # ESS below the minimum
    UNSUPPORTED = "UNSUPPORTED"  # support coverage below the minimum, or Σw = 0


@dataclass(frozen=True, slots=True)
class LoggedCandidate:
    """One row of the candidates table the planner logged with the probe (docs/08 §6)."""

    source_key: str
    eligible: bool
    tier_rank: int
    cost_paise: int
    voi_myopic_paise: int
    net_value_paise: int


@dataclass(frozen=True, slots=True)
class LoggedProbe:
    """A labelled session's first active probe."""

    session_id: UUID
    action: str
    propensity: float  # π0(action | x), as logged
    exploration_set: tuple[str, ...]  # E_i: where π0 > 0
    candidates: tuple[LoggedCandidate, ...]
    reward: float  # log2 p'(h_true) − log2 p(h_true)


@dataclass(frozen=True, slots=True)
class OpeConfig:
    bootstrap_resamples: int
    min_ess: float
    min_support_coverage: float


@dataclass(frozen=True, slots=True)
class OpeEstimate:
    policy: str
    status: OpeStatus
    value: float | None  # V̂(π); None unless status is OK
    ci_low: float | None
    ci_high: float | None
    ess: float
    n: int
    support_gap: int
    support_coverage: float


Policy = Callable[[LoggedProbe], Mapping[str, float]]
"""π(· | x_i): action → probability. Actions left out have probability 0."""


class OpeInputError(ValueError):
    """A logged row that cannot be evaluated (no propensity, or an action outside E)."""


class RebuildMismatchError(OpeInputError):
    """E_i or a* rebuilt from the stored (rounded) candidates is not what the planner used: the
    logged action or propensity disagrees with it, so the row's weights would be biased."""


# ---------------------------------------------------------------- target policies (docs/12 §3)


def _best(candidates: Sequence[LoggedCandidate], value: Callable[[LoggedCandidate], int]) -> str:
    """The eligible candidate with the highest value; ties within 1 paise by (tier, cost, key)."""
    pool = [c for c in candidates if c.eligible]
    if not pool:
        raise OpeInputError("no eligible candidate")
    top = max(value(c) for c in pool)
    ties = [c for c in pool if value(c) >= top - TIE_TOLERANCE_PAISE]
    return min(ties, key=lambda c: (c.tier_rank, c.cost_paise, c.source_key)).source_key


def greedy_action_of(candidates: Sequence[LoggedCandidate]) -> str:
    """The logged planner's greedy choice a*: the highest net value (lookahead)."""
    return _best(candidates, lambda c: c.net_value_paise)


def greedy_action(probe: LoggedProbe) -> str:
    """a* of a logged round."""
    return greedy_action_of(probe.candidates)


def uniform(probe: LoggedProbe) -> Mapping[str, float]:
    """Uniform over E_i."""
    share = 1.0 / len(probe.exploration_set)
    return dict.fromkeys(probe.exploration_set, share)


def myopic_greedy(probe: LoggedProbe) -> Mapping[str, float]:
    """Greedy on the myopic value of information net of cost (no lookahead)."""
    return {_best(probe.candidates, lambda c: c.voi_myopic_paise - c.cost_paise): 1.0}


def first_choice(source_key: str) -> Policy:
    """``source_key`` when it is eligible, else the logged greedy choice ("OTP-first")."""

    def policy(probe: LoggedProbe) -> Mapping[str, float]:
        if any(c.source_key == source_key and c.eligible for c in probe.candidates):
            return {source_key: 1.0}
        return {greedy_action(probe): 1.0}

    return policy


# ------------------------------------------------------------------------------------ SNIPS


def weights(probes: Sequence[LoggedProbe], policy: Policy) -> tuple[np.ndarray, int]:
    """``w_i`` for each probe, and how many sessions the target leaves without support."""
    w = np.zeros(len(probes), dtype=np.float64)
    gap = 0
    for i, p in enumerate(probes):
        if not p.propensity > 0 or p.action not in p.exploration_set:
            raise OpeInputError(f"session {p.session_id}: the logged action has no propensity")
        target = policy(p)
        if any(prob > 0 and a not in p.exploration_set for a, prob in target.items()):
            gap += 1  # docs/12 §3: no support in this session, w_i = 0
            continue
        w[i] = target.get(p.action, 0.0) / p.propensity
    return w, gap


def snips(w: np.ndarray, r: np.ndarray) -> float:
    """V̂(π) = Σ w_i r_i / Σ w_i."""
    return float(np.dot(w, r) / w.sum())


def effective_sample_size(w: np.ndarray) -> float:
    """(Σ w_i)² / Σ w_i²."""
    squares = float(np.dot(w, w))
    return float(w.sum()) ** 2 / squares if squares > 0 else 0.0


def evaluate(
    name: str,
    probes: Sequence[LoggedProbe],
    policy: Policy | None,
    config: OpeConfig,
    rng: np.random.Generator,
) -> OpeEstimate:
    """V̂, ESS and the 95% bootstrap CI for one target policy. ``policy=None`` is the logged
    policy: its on-policy mean (every w_i = 1)."""
    n = len(probes)
    r = np.array([p.reward for p in probes], dtype=np.float64)
    if policy is None:
        w, gap = np.ones(n, dtype=np.float64), 0
    else:
        w, gap = weights(probes, policy)
    coverage = 1.0 - gap / n if n else 0.0
    ess = effective_sample_size(w)

    def result(
        status: OpeStatus, value: float | None = None, ci: tuple[float, float] | None = None
    ) -> OpeEstimate:
        low, high = ci if ci is not None else (None, None)
        return OpeEstimate(name, status, value, low, high, ess, n, gap, coverage)

    if n == 0 or coverage < config.min_support_coverage or not w.sum() > 0:
        return result(OpeStatus.UNSUPPORTED)
    if ess < config.min_ess:
        return result(OpeStatus.INSUFFICIENT_DATA)
    return result(OpeStatus.OK, snips(w, r), _bootstrap(w, r, config.bootstrap_resamples, rng))


def _bootstrap(
    w: np.ndarray, r: np.ndarray, resamples: int, rng: np.random.Generator
) -> tuple[float, float]:
    """Percentile CI over sessions; resamples whose Σw is 0 have no estimate and are dropped."""
    n = len(w)
    picks = rng.integers(0, n, size=(resamples, n))
    total = w[picks].sum(axis=1)
    keep = total > 0
    values = (w[picks] * r[picks]).sum(axis=1)[keep] / total[keep]
    tail = (1.0 - CI_LEVEL) / 2.0
    low, high = np.quantile(values, [tail, 1.0 - tail])
    return float(low), float(high)


# ------------------------------------------------------------------------ building the rows


def true_gain(before: Mapping[str, float], after: Mapping[str, float], label: str) -> float:
    """``log2 p'(h_true) − log2 p(h_true)`` from the posteriors stored on the probe run."""
    if label not in before or label not in after:
        raise OpeInputError(f"hypothesis {label!r} is not in the stored posterior")
    if not before[label] > 0 or not after[label] > 0:
        raise OpeInputError(f"hypothesis {label!r} has zero probability")
    return math.log2(after[label]) - math.log2(before[label])


def exploration_set(candidates: Sequence[LoggedCandidate]) -> tuple[str, ...]:
    """docs/06 §10: E = {eligible, net ≥ 0, tier ≤ tier(a*)}, sorted as the planner sorts it."""
    greedy = greedy_action_of(candidates)
    tier = next(c.tier_rank for c in candidates if c.source_key == greedy)
    return tuple(
        sorted(
            c.source_key
            for c in candidates
            if c.eligible and c.net_value_paise >= 0 and c.tier_rank <= tier
        )
    )


def logged_probe(
    session_id: UUID,
    run: Mapping[str, Any],
    label: str,
    tier_rank: Mapping[str, int],
    epsilon: float,
) -> LoggedProbe:
    """A ``probe_runs`` row (as a mapping of its columns) and the session's label, as one OPE
    round. ``tier_rank`` and ``epsilon`` come from the session's pinned KB profile.

    The stored nets are rounded to the paise, so the E_i and a* rebuilt from them can differ from
    the planner's (a net of −0.4 is stored as 0; 100.6 and 99.5 become a tie). The row is checked
    against its own log: a GREEDY row's action must be the rebuilt a*, and the logged propensity
    must be ``(1 − ε)·[a = a*] + ε / |E|``. A row that disagrees raises
    ``RebuildMismatchError``; ``collect_rounds`` leaves it out and counts it."""
    if run.get("propensity") is None:
        raise OpeInputError(f"session {session_id}: the first active probe has no propensity")
    status = run.get("status")
    if status is not None and status not in COUNTED_STATUSES:
        raise OpeInputError(f"session {session_id}: the first active probe is {status}")
    if run.get("posterior_before") is None or run.get("posterior_after") is None:
        raise OpeInputError(f"session {session_id}: the first active probe stored no posterior")
    candidates = tuple(
        LoggedCandidate(
            source_key=c["source_key"],
            eligible=bool(c["eligible"]),
            tier_rank=tier_rank[c["source_key"]],
            cost_paise=int(c["cost_paise"] or 0),
            voi_myopic_paise=int(c["voi_myopic_paise"] or 0),
            net_value_paise=int(c["net_value_paise"] or 0),
        )
        for c in run["candidates"]
    )
    action, propensity = str(run["probe_key"]), float(run["propensity"])
    pool = exploration_set(candidates)
    greedy = greedy_action_of(candidates)
    if run.get("selected_by") == "GREEDY" and action != greedy:
        raise RebuildMismatchError(
            f"session {session_id}: logged greedy {action}, rebuilt a* is {greedy}"
        )
    expected = (1.0 - epsilon) * (1.0 if action == greedy else 0.0) + epsilon / len(pool)
    if action not in pool or abs(propensity - expected) > PROPENSITY_TOLERANCE:
        raise RebuildMismatchError(
            f"session {session_id}: logged propensity {propensity}, rebuilt {expected}"
            f" over E = {pool}"
        )
    return LoggedProbe(
        session_id=session_id,
        action=action,
        propensity=propensity,
        exploration_set=pool,
        candidates=candidates,
        reward=true_gain(run["posterior_before"], run["posterior_after"], label),
    )


@dataclass(frozen=True, slots=True)
class RoundRow:
    """What ``logged_probe`` needs for one labelled session."""

    session_id: UUID
    run: Mapping[str, Any]
    label: str
    tier_rank: Mapping[str, int]
    epsilon: float


@dataclass(frozen=True, slots=True)
class LoggedRounds:
    probes: tuple[LoggedProbe, ...]
    rebuild_mismatch: int  # rows left out: the rebuilt E_i or a* disagrees with the log


def collect_rounds(rows: Iterable[RoundRow]) -> LoggedRounds:
    """The OPE rounds, leaving out (and counting) the rows whose rebuild disagrees with the
    log, so one such row doesn't stop the evaluation. Other bad rows still raise."""
    probes: list[LoggedProbe] = []
    mismatch = 0
    for row in rows:
        try:
            probes.append(
                logged_probe(row.session_id, row.run, row.label, row.tier_rank, row.epsilon)
            )
        except RebuildMismatchError:
            mismatch += 1
    return LoggedRounds(tuple(probes), mismatch)
