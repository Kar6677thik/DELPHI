"""The anomaly detector: an isolation forest served as a percentile ``q`` (docs/13 §5, FR-SNS-04).

The forest is trained on the payment features of training-window payments labelled GENUINE.
A payment's anomaly score is ``s = −score_samples(x)`` (higher = more anomalous), and

    q = #{training quantiles ≤ s} / n_quantiles      ∈ [0, 1]

over the sorted table of ``n_quantiles`` (10,001) quantiles of the training scores stored with
the model. ``q`` never decreases as ``s`` rises. It feeds the Guardian prior's open mass and the
fast-path rule (docs/06 §11, docs/08 §3).

Choosing the training rows (the matured window and observed labels, docs/13 §4) and storing
the artifact (``model_artifacts``, docs/13 §7) belong to the training job, not to this module.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import joblib
import numpy as np
from numpy.typing import NDArray
from sklearn.ensemble import IsolationForest

from delphi.sensing.config import AnomalyConfig

FloatMatrix = NDArray[np.float64]
_BLOB_FORMAT = 1
_MATRIX_NDIM = 2


class AnomalyInputError(ValueError):
    """Features that cannot be scored: missing, unexpected or not finite."""


def feature_matrix(rows: Sequence[Mapping[str, float]], feature_list: Sequence[str]) -> FloatMatrix:
    """Rows of feature dicts → a float64 matrix whose columns follow ``feature_list``."""
    expected = set(feature_list)
    matrix = np.empty((len(rows), len(feature_list)), dtype=np.float64)
    for i, row in enumerate(rows):
        if set(row) != expected:
            missing = sorted(expected - set(row))
            extra = sorted(set(row) - expected)
            raise AnomalyInputError(f"feature mismatch: missing {missing}, unexpected {extra}")
        matrix[i] = [row[name] for name in feature_list]
    if not np.all(np.isfinite(matrix)):
        raise AnomalyInputError("features must be finite")
    return matrix


@dataclass(frozen=True, slots=True, eq=False)
class AnomalyModel:
    """A trained forest, its feature order and its sorted training-score quantiles."""

    forest: IsolationForest
    feature_list: tuple[str, ...]
    quantiles: FloatMatrix  # shape (n_quantiles,), non-decreasing
    n_train: int

    def score(self, features: Mapping[str, float]) -> float:
        """The anomaly score ``−score_samples`` of one payment (higher = more anomalous)."""
        return float(self.scores(feature_matrix([features], self.feature_list))[0])

    def scores(self, matrix: FloatMatrix) -> FloatMatrix:
        result: FloatMatrix = -np.asarray(self.forest.score_samples(matrix), dtype=np.float64)
        return result

    def percentile_of_score(self, score: float) -> float:
        """``q`` for an anomaly score: the share of training quantiles ≤ ``score``."""
        index = int(np.searchsorted(self.quantiles, score, side="right"))
        return index / len(self.quantiles)

    def percentile(self, features: Mapping[str, float]) -> float:
        """``q ∈ [0, 1]`` for one payment's features."""
        return self.percentile_of_score(self.score(features))

    def metrics(self) -> dict[str, float]:
        """Stored with the artifact: the training size and the training-score distribution."""
        last = len(self.quantiles) - 1
        return {
            "n_train": float(self.n_train),
            "score_p50": float(self.quantiles[last // 2]),
            "score_p90": float(self.quantiles[round(last * 0.9)]),
            "score_p99": float(self.quantiles[round(last * 0.99)]),
        }

    def to_bytes(self) -> bytes:
        """The artifact blob (joblib). Load it only from DELPHI's own ``model_artifacts``."""
        buffer = io.BytesIO()
        joblib.dump(
            {
                "format": _BLOB_FORMAT,
                "forest": self.forest,
                "feature_list": list(self.feature_list),
                "quantiles": self.quantiles,
                "n_train": self.n_train,
            },
            buffer,
        )
        return buffer.getvalue()

    @classmethod
    def from_bytes(cls, blob: bytes) -> AnomalyModel:
        payload: dict[str, Any] = joblib.load(io.BytesIO(blob))
        if payload.get("format") != _BLOB_FORMAT:
            raise ValueError(f"unknown anomaly artifact format {payload.get('format')!r}")
        return cls(
            forest=payload["forest"],
            feature_list=tuple(payload["feature_list"]),
            quantiles=np.asarray(payload["quantiles"], dtype=np.float64),
            n_train=int(payload["n_train"]),
        )


_LEAF = -1  # sklearn's TREE_LEAF child index


def _average_path_length(n: NDArray[np.float64]) -> NDArray[np.float64]:
    """c(n), the average path length of an unsuccessful BST search (as sklearn computes it)."""
    n = np.asarray(n, dtype=np.float64)
    result = np.zeros_like(n)
    result[n == 2.0] = 1.0  # noqa: PLR2004 - c(2) = 1 by definition
    big = n > 2.0  # noqa: PLR2004
    result[big] = 2.0 * (np.log(n[big] - 1.0) + np.euler_gamma) - 2.0 * (n[big] - 1.0) / n[big]
    return result


@dataclass(frozen=True, slots=True, eq=False)
class FlatForest:
    """The forest's trees as flat arrays, to score one payment in well under a millisecond
    (``score_samples`` walks the trees one Python call at a time: ~20 ms for 200 trees, over
    the inference budget, docs/13 §7). It gives what ``AnomalyModel.score`` gives: all trees
    are walked together, each node comparing the float32 feature against its threshold as
    sklearn does, and a leaf adds its depth plus c(samples in the leaf)."""

    roots: NDArray[np.int64]
    left: NDArray[np.int64]  # global node index, _LEAF at a leaf
    right: NDArray[np.int64]
    feature: NDArray[np.int64]  # the column in the model's feature list
    threshold: NDArray[np.float64]
    leaf_depth: NDArray[np.float64]  # depth + c(n_node_samples), at leaves
    denominator: float  # n_trees × c(max_samples)

    @classmethod
    def of(cls, forest: IsolationForest) -> FlatForest:
        roots, left, right, feature, threshold, leaf_depth = [], [], [], [], [], []
        offset = 0
        for tree, columns in zip(forest.estimators_, forest.estimators_features_, strict=True):
            t = tree.tree_
            depth = np.zeros(t.node_count, dtype=np.float64)
            for node in range(t.node_count):  # children always follow their parent
                for child in (t.children_left[node], t.children_right[node]):
                    if child != _LEAF:
                        depth[child] = depth[node] + 1.0
            is_leaf = t.children_left == _LEAF
            roots.append(offset)
            left.append(np.where(is_leaf, _LEAF, t.children_left + offset))
            right.append(np.where(is_leaf, _LEAF, t.children_right + offset))
            feature.append(np.where(is_leaf, 0, np.asarray(columns)[np.maximum(t.feature, 0)]))
            threshold.append(t.threshold)
            n_samples = np.asarray(t.n_node_samples, dtype=np.float64)
            leaf_depth.append(depth + _average_path_length(n_samples))
            offset += t.node_count
        max_samples = np.array([forest.max_samples_], dtype=np.float64)
        return cls(
            roots=np.asarray(roots, dtype=np.int64),
            left=np.concatenate(left).astype(np.int64),
            right=np.concatenate(right).astype(np.int64),
            feature=np.concatenate(feature).astype(np.int64),
            threshold=np.concatenate(threshold).astype(np.float64),
            leaf_depth=np.concatenate(leaf_depth),
            denominator=float(len(forest.estimators_) * _average_path_length(max_samples)[0]),
        )

    def score(self, row: NDArray[np.float64]) -> float:
        """The anomaly score of one feature row (columns in the model's feature list)."""
        x = np.asarray(row, dtype=np.float32).astype(np.float64)
        node = self.roots.copy()
        while True:
            inner = self.left[node] != _LEAF
            if not inner.any():
                break
            at = node[inner]
            go_left = x[self.feature[at]] <= self.threshold[at]
            node[inner] = np.where(go_left, self.left[at], self.right[at])
        depths = 0.0
        for d in self.leaf_depth[node].tolist():  # tree by tree, in sklearn's order
            depths += d
        if self.denominator == 0:
            return 1.0
        return float(2.0 ** (-depths / self.denominator))


def _check_feature_list(feature_list: Sequence[str]) -> None:
    if not feature_list:
        raise AnomalyInputError("the feature list is empty")
    if len(set(feature_list)) != len(feature_list):
        raise AnomalyInputError("the feature list has duplicates")


def train_anomaly(
    rows: Sequence[Mapping[str, float]], feature_list: Sequence[str], config: AnomalyConfig
) -> AnomalyModel:
    """Fit the forest on GENUINE training payments and build the quantile table.

    Deterministic for a fixed ``config.random_state``.
    """
    _check_feature_list(feature_list)
    if not rows:
        raise AnomalyInputError("no training payments")
    return _fit(feature_matrix(rows, feature_list), feature_list, config)


def train_anomaly_matrix(
    matrix: FloatMatrix, feature_list: Sequence[str], config: AnomalyConfig
) -> AnomalyModel:
    """``train_anomaly`` on a ready feature matrix whose columns follow ``feature_list`` (the
    training job's path: the prior model's matrix, docs/13 §4–5)."""
    _check_feature_list(feature_list)
    if matrix.ndim != _MATRIX_NDIM or matrix.shape[1] != len(feature_list):
        raise AnomalyInputError("the matrix needs one column per feature")
    if len(matrix) == 0:
        raise AnomalyInputError("no training payments")
    if not np.all(np.isfinite(matrix)):
        raise AnomalyInputError("features must be finite")
    return _fit(matrix, feature_list, config)


def _fit(matrix: FloatMatrix, feature_list: Sequence[str], config: AnomalyConfig) -> AnomalyModel:
    """The forest and its quantile table, on a checked matrix."""
    forest = IsolationForest(
        n_estimators=config.n_estimators,
        contamination=config.contamination,
        random_state=config.random_state,
    )
    forest.fit(matrix)
    train_scores = -np.asarray(forest.score_samples(matrix), dtype=np.float64)
    quantiles = np.quantile(train_scores, np.linspace(0.0, 1.0, config.n_quantiles))
    return AnomalyModel(
        forest=forest,
        feature_list=tuple(feature_list),
        quantiles=np.asarray(quantiles, dtype=np.float64),
        n_train=len(matrix),
    )
