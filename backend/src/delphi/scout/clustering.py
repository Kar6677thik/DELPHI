"""The Scout's clustering core (docs/11 §2, FR-SCT-01, FR-SCT-02): pure functions over queue
items, with no I/O. The ``RUN_SCOUT`` job loads the items and stores what this returns.

- **Feature vector per item:** each source's outcome one-hot plus a ``not_observed`` indicator;
  the doc 13 §3 ``scout`` payment features, z-scored with the sensing model's training means and
  standard deviations; the channel one-hot; and the F3 stage indicators of the customer's last
  24 h.
- **Clustering:** scikit-learn ``HDBSCAN`` with the KB's ``scout.min_cluster_size`` and
  ``min_samples`` (Euclidean). Noise (-1) stays in the queue.
- **Per cluster:** the top 8 distinguishing features by absolute standardised mean difference
  against the known-typology items of the same period; the outcome profile (empirical
  frequencies per observed source); draft likelihood rows, Laplace-smoothed, with ESS
  ``min(n + |O|, default_ess)``, for observed sources only (unobserved ones stay uniform, doc 06
  §2 rule 3); and the template name "Candidate pattern {n}: {top 3 features}".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sklearn.cluster import HDBSCAN

NOISE = -1
TOP_FEATURES = 8  # docs/11 §2
NAME_FEATURES = 3  # the template name lists the top 3
NOT_OBSERVED = "not_observed"
_EPS = 1e-9

FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class ScoutItem:
    """One queue item (a Guardian session or a case) as the Scout sees it."""

    id: str
    outcomes: Mapping[str, str]  # source → outcome, for the sources it observed
    features: Mapping[str, float]  # the doc 13 §3 `scout` payment features, raw
    channel: str
    f3_stages: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class FeatureSpace:
    """The columns of the Scout's matrix, fixed for one run."""

    sources: Mapping[str, Sequence[str]]  # source → its outcomes (KB order)
    feature_means: Mapping[str, float]  # the training means of the scout features
    feature_stds: Mapping[str, float]
    channels: Sequence[str]
    f3_stages: Sequence[str]

    @property
    def columns(self) -> list[str]:
        cols = [f"{s}={o}" for s, outs in self.sources.items() for o in (*outs, NOT_OBSERVED)]
        cols += [f"z:{f}" for f in self.feature_means]
        cols += [f"channel={c}" for c in self.channels]
        cols += [f"f3={stage}" for stage in self.f3_stages]
        return cols


def matrix(items: Sequence[ScoutItem], space: FeatureSpace) -> FloatArray:
    """docs/11 §2's feature vector for each item, one row each."""
    rows: list[list[float]] = []
    for item in items:
        row: list[float] = []
        for source, outcomes in space.sources.items():
            seen = item.outcomes.get(source)
            row += [1.0 if seen == o else 0.0 for o in outcomes]
            row.append(1.0 if seen is None else 0.0)
        for name, mean in space.feature_means.items():
            std = space.feature_stds.get(name, 0.0)
            value = float(item.features.get(name, mean))
            row.append((value - mean) / std if std > _EPS else 0.0)
        row += [1.0 if item.channel == c else 0.0 for c in space.channels]
        row += [1.0 if stage in item.f3_stages else 0.0 for stage in space.f3_stages]
        rows.append(row)
    return np.asarray(rows, dtype=np.float64).reshape(len(items), len(space.columns))


def cluster_labels(x: FloatArray, *, min_cluster_size: int, min_samples: int) -> NDArray[np.int64]:
    """HDBSCAN labels (``NOISE`` for points in no cluster). Fewer items than a cluster needs:
    all noise."""
    if len(x) < min_cluster_size:
        return np.full(len(x), NOISE, dtype=np.int64)
    model = HDBSCAN(
        min_cluster_size=min_cluster_size, min_samples=min_samples, metric="euclidean", copy=True
    )
    labels: NDArray[np.int64] = model.fit_predict(x).astype(np.int64)
    return labels


def distinguishing_features(
    cluster: FloatArray, reference: FloatArray, columns: Sequence[str], top: int = TOP_FEATURES
) -> list[dict[str, float | str]]:
    """The ``top`` columns by absolute standardised mean difference, cluster vs reference:
    (mean_c − mean_r) / sqrt((var_c + var_r) / 2), with columns constant in both left out."""
    mean_c, mean_r = cluster.mean(axis=0), reference.mean(axis=0)
    pooled = np.sqrt((cluster.var(axis=0) + reference.var(axis=0)) / 2)
    diff = mean_c - mean_r
    smd = np.where(pooled > _EPS, diff / np.maximum(pooled, _EPS), 0.0)
    order = np.argsort(-np.abs(smd), kind="stable")
    return [
        {
            "feature": columns[i],
            "smd": round(float(smd[i]), 4),
            "cluster_mean": round(float(mean_c[i]), 4),
            "reference_mean": round(float(mean_r[i]), 4),
        }
        for i in order[:top]
        if abs(smd[i]) > 0
    ]


def outcome_profile(
    members: Sequence[ScoutItem], sources: Mapping[str, Sequence[str]]
) -> dict[str, dict[str, float]]:
    """For each source the cluster observed: its empirical outcome frequencies."""
    profile: dict[str, dict[str, float]] = {}
    for source, outcomes in sources.items():
        seen = Counter(m.outcomes[source] for m in members if source in m.outcomes)
        n = sum(seen.values())
        if n:
            profile[source] = {o: seen[o] / n for o in outcomes}
    return profile


def draft_likelihoods(
    members: Sequence[ScoutItem], sources: Mapping[str, Sequence[str]], *, max_ess: float
) -> dict[str, dict[str, Any]]:
    """docs/11 §2: (counts + 1) / (n + |O|), ESS = min(n + |O|, max_ess); no row for a source
    the cluster never observed (it stays uniform, doc 06 §2 rule 3)."""
    rows: dict[str, dict[str, Any]] = {}
    for source, outcomes in sources.items():
        seen = Counter(m.outcomes[source] for m in members if source in m.outcomes)
        n = sum(seen.values())
        if n == 0:
            continue
        k = len(outcomes)
        rows[source] = {
            "probs": [(seen[o] + 1) / (n + k) for o in outcomes],
            "ess": float(min(n + k, max_ess)),
        }
    return rows


def template_name(number: int, distinguishing: Sequence[Mapping[str, Any]]) -> str:
    """docs/11 §2's fallback name: "Candidate pattern {n}: {top 3 distinguishing features}"."""
    top = ", ".join(str(d["feature"]) for d in distinguishing[:NAME_FEATURES])
    return f"Candidate pattern {number}: {top}" if top else f"Candidate pattern {number}"


@dataclass(frozen=True, slots=True)
class Cluster:
    number: int
    member_ids: tuple[str, ...]
    distinguishing_features: list[dict[str, float | str]]
    outcome_profile: dict[str, dict[str, float]]
    draft_likelihoods: dict[str, dict[str, Any]]
    suggested_name: str


@dataclass(frozen=True, slots=True)
class ScoutResult:
    clusters: tuple[Cluster, ...]
    noise_ids: tuple[str, ...] = field(default_factory=tuple)


def scout(
    queue: Sequence[ScoutItem],
    reference: Sequence[ScoutItem],
    space: FeatureSpace,
    *,
    min_cluster_size: int,
    min_samples: int,
    max_ess: float,
) -> ScoutResult:
    """One Scout run over the queue (the last ``lookback_days``), against the known-typology
    items of the same period."""
    if not queue:
        return ScoutResult(())
    x = matrix(queue, space)
    labels = cluster_labels(x, min_cluster_size=min_cluster_size, min_samples=min_samples)
    ref = matrix(reference, space) if reference else np.zeros((1, x.shape[1]))
    columns = space.columns
    clusters = []
    for number, label in enumerate(sorted(set(labels.tolist()) - {NOISE}), start=1):
        mask = labels == label
        members = [item for item, m in zip(queue, mask, strict=True) if m]
        distinguishing = distinguishing_features(x[mask], ref, columns)
        clusters.append(
            Cluster(
                number=number,
                member_ids=tuple(m.id for m in members),
                distinguishing_features=distinguishing,
                outcome_profile=outcome_profile(members, space.sources),
                draft_likelihoods=draft_likelihoods(members, space.sources, max_ess=max_ess),
                suggested_name=template_name(number, distinguishing),
            )
        )
    noise = tuple(item.id for item, label in zip(queue, labels, strict=True) if label == NOISE)
    return ScoutResult(tuple(clusters), noise)
