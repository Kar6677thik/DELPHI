"""The docs/22 §4 metrics of one policy's run, with 95% cluster-bootstrap CIs over customers.

The definitions are normative (docs/22 §4): never change them to flatter a result. Each metric
is a weighted statistic over the payments, so a bootstrap replicate only reweights them: a
customer drawn k times gives each of their payments weight k (docs/22 §1 step 7). The point
estimate is the same statistic with every weight 1.

Quantiles (hold resolution time, the CI bounds) use the inverted CDF, with no interpolation: a
hold the run ends on counts as +∞ (it is still unresolved, the conservative reading), and an
interpolated quantile between a finite value and +∞ is not defined.

Not here yet: uncertainty reduced per investigator minute (the queue, #124 part 2), and the
golden-hour recovery ratio, tracing delay and wrongful liens (Chase, #124 part 3).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import numpy as np
from numpy.typing import NDArray

from delphi.evaluation.config import EvaluationConfig
from delphi.evaluation.outcomes import (
    Decision,
    LabelRecord,
    PaymentOutcome,
    ProbeMode,
)
from delphi.sim.scenarios import ScenarioType

# docs/22 §4: the coercion catch rate is over these scenarios, among payments whose OTP passes.
COERCION_SCENARIOS = frozenset({ScenarioType.APP_DIGITAL_ARREST, ScenarioType.APP_INVESTMENT})
# docs/12 §4: the decisions whose label coverage is reported.
HELD_DECISIONS = frozenset({Decision.HOLD_FOR_REVIEW, Decision.PROTECTIVE_HOLD, Decision.DECLINE})
INFO_YIELD = "info_yield_bits"  # one metric per probe key: info_yield_bits[<probe key>]
_SECONDS_PER_MINUTE = 60.0
_MEDIAN, _P90 = 0.5, 0.9

Floats = NDArray[np.float64]
Bools = NDArray[np.bool_]


@dataclass(frozen=True, slots=True)
class Estimate:
    """A metric's value and its CI. None where the metric is undefined (an empty denominator,
    or a field the policy does not produce)."""

    value: float | None
    low: float | None
    high: float | None


@dataclass(frozen=True, slots=True)
class _Frame:
    """The run as columns, one row per payment, plus one row per probe."""

    customer: NDArray[np.intp]  # index into the sorted customer ids
    n_customers: int
    amount: Floats
    fraud: Bools
    stopped: Bools
    coercion: Bools  # a coercion scenario whose OTP would pass
    vulnerable: Bools
    any_hold: Bools  # decided anything but RELEASE
    held: Bools  # entered the investigation queue
    resolution_minutes: Floats  # +inf if unresolved, nan if never held
    labelled: Bools  # docs/12 §4 held decision with a label known in time
    coverage_eligible: Bools
    friction: Floats
    active_probes: Floats
    attributed: Bools  # argmax hypothesis = true hypothesis
    attribution_eligible: Bools  # stopped fraud whose true hypothesis is in the KB
    dd_complete: Bools
    dd_reported: Bools
    probe_row: NDArray[np.intp]  # the payment each probe ran on
    probe_yield: Floats
    probes_by_key: Mapping[str, NDArray[np.intp]]  # the observed probes of each key


def _frame(
    outcomes: Iterable[PaymentOutcome],
    labels: Iterable[LabelRecord],
    *,
    kb_hypotheses: Collection[str],
    config: EvaluationConfig,
) -> _Frame:
    rows = list(outcomes)
    known: dict[UUID, list[LabelRecord]] = {}
    for label in labels:
        known.setdefault(label.payment_id, []).append(label)
    window = timedelta(days=config.label_coverage_days)
    ids = sorted({o.customer_id for o in rows})
    index = {c: i for i, c in enumerate(ids)}

    def floats(f: Callable[[PaymentOutcome], float]) -> Floats:
        return np.fromiter((f(o) for o in rows), dtype=np.float64, count=len(rows))

    def bools(f: Callable[[PaymentOutcome], bool]) -> Bools:
        return np.fromiter((f(o) for o in rows), dtype=np.bool_, count=len(rows))

    def resolution(o: PaymentOutcome) -> float:
        if o.held_at is None:
            return math.nan
        if o.resolved_at is None:
            return math.inf
        return (o.resolved_at - o.held_at).total_seconds() / _SECONDS_PER_MINUTE

    def labelled(o: PaymentOutcome) -> bool:
        deadline = o.decided_at + window
        return any(label.known_at <= deadline for label in known.get(o.payment_id, ()))

    probes = [(i, p) for i, o in enumerate(rows) for p in o.probes]
    by_key: dict[str, list[int]] = {}
    for n, (_, p) in enumerate(probes):
        if p.info_yield_bits is not None:
            by_key.setdefault(p.probe_key, []).append(n)
    return _Frame(
        customer=np.fromiter((index[o.customer_id] for o in rows), np.intp, len(rows)),
        n_customers=len(ids),
        amount=floats(lambda o: o.amount_paise),
        fraud=bools(lambda o: o.is_fraud),
        stopped=bools(lambda o: o.stopped),
        coercion=bools(lambda o: o.scenario in COERCION_SCENARIOS and o.otp_would_pass),
        vulnerable=bools(lambda o: o.vulnerable),
        any_hold=bools(lambda o: o.decision is not Decision.RELEASE),
        held=bools(lambda o: o.held_at is not None),
        resolution_minutes=floats(resolution),
        labelled=bools(labelled),
        coverage_eligible=bools(lambda o: o.decision in HELD_DECISIONS),
        friction=floats(lambda o: sum(p.friction_paise for p in o.probes)),
        active_probes=floats(lambda o: sum(p.mode is ProbeMode.ACTIVE for p in o.probes)),
        attributed=bools(lambda o: o.argmax_hypothesis == o.true_hypothesis),
        attribution_eligible=bools(
            lambda o: o.is_fraud and o.stopped and o.true_hypothesis in kb_hypotheses
        ),
        dd_complete=bools(lambda o: o.dd_pack_complete is True),
        dd_reported=bools(lambda o: o.dd_pack_complete is not None),
        probe_row=np.fromiter((i for i, _ in probes), np.intp, len(probes)),
        probe_yield=np.fromiter((p.info_yield_bits or 0.0 for _, p in probes), np.float64),
        probes_by_key={k: np.asarray(v, dtype=np.intp) for k, v in sorted(by_key.items())},
    )


def _ratio(num: float, den: float) -> float | None:
    return num / den if den > 0 else None


def _share(w: Floats, hit: Bools, among: Bools) -> float | None:
    return _ratio(float(w[among & hit].sum()), float(w[among].sum()))


def weighted_quantile(values: Floats, weights: Floats, q: float) -> float | None:
    """The inverted-CDF quantile: the smallest value whose cumulative weight reaches ``q`` of
    the total (``numpy.quantile(method="inverted_cdf")`` on the values repeated by weight)."""
    keep = weights > 0
    values, weights = values[keep], weights[keep]
    if values.size == 0:
        return None
    order = np.argsort(values, kind="stable")
    cumulative = np.cumsum(weights[order])
    at = int(np.searchsorted(cumulative, q * cumulative[-1], side="left"))
    return float(values[order][min(at, values.size - 1)])


def _statistics(frame: _Frame, w: Floats) -> dict[str, float | None]:
    """Every metric under per-payment weights ``w``."""
    fraud_w = w * frame.fraud
    prevented = float((fraud_w * frame.stopped * frame.amount).sum())
    genuine = ~frame.fraud
    held_w = np.where(frame.held, w, 0.0)
    out: dict[str, float | None] = {
        "fraud_prevented_paise": prevented,
        "fraud_recall_count": _share(w, frame.stopped, frame.fraud),
        "fraud_recall_amount": _ratio(prevented, float((fraud_w * frame.amount).sum())),
        "coercion_catch_rate": _share(w, frame.stopped, frame.coercion),
        "genuine_hold_rate": _share(w, frame.any_hold, genuine),
        "genuine_hold_rate_vulnerable": _share(w, frame.any_hold, genuine & frame.vulnerable),
        "hold_resolution_minutes_median": weighted_quantile(
            frame.resolution_minutes, held_w, _MEDIAN
        ),
        "hold_resolution_minutes_p90": weighted_quantile(frame.resolution_minutes, held_w, _P90),
        "friction_per_fraud_paise_prevented": _ratio(float((w * frame.friction).sum()), prevented),
        "active_probes_per_genuine_payment": _ratio(
            float((w * frame.active_probes)[genuine].sum()), float(w[genuine].sum())
        ),
        "label_coverage_held": _share(w, frame.labelled, frame.coverage_eligible),
        "dd_pack_completeness": _share(w, frame.dd_complete, frame.dd_reported),
        "typology_attribution_accuracy": _share(w, frame.attributed, frame.attribution_eligible),
    }
    probe_w = w[frame.probe_row]
    for key, at in frame.probes_by_key.items():
        out[f"{INFO_YIELD}[{key}]"] = _ratio(
            float((probe_w[at] * frame.probe_yield[at]).sum()), float(probe_w[at].sum())
        )
    return out


def _interval(replicates: list[float | None], level: float) -> tuple[float | None, float | None]:
    defined = np.asarray([r for r in replicates if r is not None], dtype=np.float64)
    if defined.size == 0:
        return None, None
    tail = (1 - level) / 2
    low, high = np.quantile(defined, [tail, 1 - tail], method="inverted_cdf")
    return float(low), float(high)


def evaluate_policy(
    outcomes: Iterable[PaymentOutcome],
    labels: Iterable[LabelRecord],
    *,
    kb_hypotheses: Collection[str],
    config: EvaluationConfig,
) -> Mapping[str, Estimate]:
    """One policy's docs/22 §4 metrics, each with its percentile CI from
    ``config.bootstrap.replicates`` customer resamples. ``kb_hypotheses`` are KB v1's keys
    (typology attribution counts only those, docs/22 §4)."""
    frame = _frame(outcomes, labels, kb_hypotheses=kb_hypotheses, config=config)
    point = _statistics(frame, np.ones(frame.customer.size))
    boot = config.bootstrap
    rng = np.random.default_rng(boot.seed)
    replicates: dict[str, list[float | None]] = {name: [] for name in point}
    for _ in range(boot.replicates):
        drawn = rng.integers(0, max(frame.n_customers, 1), frame.n_customers)
        counts = np.bincount(drawn, minlength=frame.n_customers).astype(np.float64)
        for name, value in _statistics(frame, counts[frame.customer]).items():
            replicates[name].append(value)
    return {
        name: Estimate(value, *_interval(replicates[name], boot.level))
        for name, value in point.items()
    }
