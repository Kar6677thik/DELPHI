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


def train_anomaly(
    rows: Sequence[Mapping[str, float]], feature_list: Sequence[str], config: AnomalyConfig
) -> AnomalyModel:
    """Fit the forest on GENUINE training payments and build the quantile table.

    Deterministic for a fixed ``config.random_state``.
    """
    if not feature_list:
        raise AnomalyInputError("the feature list is empty")
    if len(set(feature_list)) != len(feature_list):
        raise AnomalyInputError("the feature list has duplicates")
    if not rows:
        raise AnomalyInputError("no training payments")
    matrix = feature_matrix(rows, feature_list)
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
        n_train=len(rows),
    )
