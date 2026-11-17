"""The mule scorer: ``p_mule(account)`` from the account features (docs/13 §6, FR-SNS-05).

A LightGBM binary classifier on ``ACCOUNT_FEATURES``, calibrated by isotonic regression on the
validation split, and gated on its test-split AUC. Choosing the labelled accounts, their
``as_of`` and their split belongs to the training job (``delphi.sensing.trainers``); storing
the artifact belongs to ``TRAIN_SENSING`` (docs/13 §7).
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score

from delphi.sensing.config import MuleConfig, MuleGate

FloatMatrix = NDArray[np.float64]
IntArray = NDArray[np.int64]
_BLOB_FORMAT = 1
_MATRIX_NDIM = 2


class MuleInputError(ValueError):
    """Training or scoring input the mule scorer cannot use."""


@dataclass(frozen=True, slots=True)
class MuleSplit:
    """One split's account feature matrix (columns follow the feature list) and 0/1 labels."""

    features: FloatMatrix
    labels: IntArray

    def __post_init__(self) -> None:
        if self.features.ndim != _MATRIX_NDIM or len(self.features) != len(self.labels):
            raise MuleInputError("a split needs one label per feature row")
        if len(self.labels) and not np.isin(self.labels, (0, 1)).all():
            raise MuleInputError("labels must be 0 (not mule) or 1 (mule)")
        if not np.all(np.isfinite(self.features)):
            raise MuleInputError("features must be finite")

    def __len__(self) -> int:
        return len(self.labels)


@dataclass(frozen=True, slots=True)
class MuleModel:
    booster: lgb.Booster
    feature_list: tuple[str, ...]
    calibration: IsotonicRegression | None  # None: no validation split with both classes
    report: Mapping[str, Any] = field(default_factory=dict)

    def scores(self, matrix: FloatMatrix) -> FloatMatrix:
        """Calibrated ``p_mule`` for each row, in [0, 1]."""
        if matrix.ndim != _MATRIX_NDIM or matrix.shape[1] != len(self.feature_list):
            raise MuleInputError("the matrix needs one column per feature")
        if not np.all(np.isfinite(matrix)):
            raise MuleInputError("features must be finite")
        raw = np.asarray(self.booster.predict(matrix), dtype=np.float64)
        if self.calibration is not None:
            raw = np.asarray(self.calibration.predict(raw), dtype=np.float64)
        result: FloatMatrix = np.clip(raw, 0.0, 1.0)
        return result

    def p_mule(self, features: Mapping[str, float]) -> float:
        """``p_mule`` for one account's features (``ACCOUNT_FEATURES``)."""
        if set(features) != set(self.feature_list):
            raise MuleInputError("feature mismatch")
        row = np.array([[features[f] for f in self.feature_list]], dtype=np.float64)
        return float(self.scores(row)[0])

    def to_bytes(self) -> bytes:
        """The artifact blob (joblib). Load it only from DELPHI's own ``model_artifacts``."""
        buffer = io.BytesIO()
        joblib.dump(
            {
                "format": _BLOB_FORMAT,
                "booster": self.booster.model_to_string(),
                "feature_list": list(self.feature_list),
                "calibration": self.calibration,
                "report": dict(self.report),
            },
            buffer,
        )
        return buffer.getvalue()

    @classmethod
    def from_bytes(cls, blob: bytes) -> MuleModel:
        payload: dict[str, Any] = joblib.load(io.BytesIO(blob))
        if payload.get("format") != _BLOB_FORMAT:
            raise ValueError(f"unknown mule artifact format {payload.get('format')!r}")
        return cls(
            booster=lgb.Booster(model_str=payload["booster"]),
            feature_list=tuple(payload["feature_list"]),
            calibration=payload["calibration"],
            report=payload["report"],
        )


@dataclass(frozen=True, slots=True)
class MuleGateResult:
    enforced: bool
    passed: bool
    reasons: tuple[str, ...]


def auc(scores: FloatMatrix, labels: IntArray) -> float | None:
    """ROC AUC, or ``None`` when the split lacks mules or non-mules."""
    if not 0 < int(labels.sum()) < len(labels):
        return None
    return float(roc_auc_score(labels, scores))


def mule_gate(test_auc: float | None, profile: str, config: MuleGate) -> MuleGateResult:
    """docs/13 §6: AUC ≥ ``min_auc`` on the test split, enforced only on
    ``config.enforced_profiles``; elsewhere it is reported and ``passed`` is true."""
    reasons: list[str] = []
    if test_auc is None:
        reasons.append("test AUC undefined (the test split needs mules and non-mules)")
    elif test_auc < config.min_auc:
        reasons.append(f"AUC {test_auc:.4f} < {config.min_auc}")
    enforced = profile in config.enforced_profiles
    return MuleGateResult(enforced, not reasons or not enforced, tuple(reasons))


def _split_report(model: MuleModel, split: MuleSplit) -> dict[str, Any]:
    scores = model.scores(split.features) if len(split) else np.empty(0)
    return {
        "n": len(split),
        "mules": int(split.labels.sum()),
        "auc": auc(scores, split.labels) if len(split) else None,
    }


def train_mule(
    train: MuleSplit,
    validation: MuleSplit,
    test: MuleSplit,
    feature_list: Sequence[str],
    config: MuleConfig,
    *,
    profile: str,
) -> tuple[MuleModel, MuleGateResult]:
    """Fit the booster on ``train``, the isotonic calibration on ``validation`` (when it has
    both classes), and report and gate on ``test``. Deterministic for a fixed ``config.seed``."""
    if not feature_list or len(set(feature_list)) != len(feature_list):
        raise MuleInputError("the feature list is empty or has duplicates")
    for split in (train, validation, test):
        if split.features.shape[1] != len(feature_list):
            raise MuleInputError("a split's columns do not match the feature list")
    if not 0 < int(train.labels.sum()) < len(train):
        raise MuleInputError("the training split needs mules and non-mules")
    params = {
        "objective": "binary",
        "num_leaves": config.num_leaves,
        "learning_rate": config.learning_rate,
        "seed": config.seed,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 1,
        "verbosity": -1,
    }
    data = lgb.Dataset(
        train.features, label=train.labels, feature_name=list(feature_list), free_raw_data=False
    )
    booster = lgb.train(params, data, num_boost_round=config.n_estimators)
    calibration: IsotonicRegression | None = None
    if len(validation) and 0 < int(validation.labels.sum()) < len(validation):
        raw = np.asarray(booster.predict(validation.features), dtype=np.float64)
        calibration = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        calibration.fit(raw, validation.labels)
    model = MuleModel(booster, tuple(feature_list), calibration)
    test_report = _split_report(model, test)
    result = mule_gate(test_report["auc"], profile, config.gate)
    report = {
        "profile": profile,
        "train": _split_report(model, train),
        "validation": _split_report(model, validation),
        "test": test_report,
        "calibrated": calibration is not None,
        "gate": {
            "enforced": result.enforced,
            "passed": result.passed,
            "reasons": list(result.reasons),
        },
    }
    return MuleModel(booster, tuple(feature_list), calibration, report), result
