"""The prior model: LightGBM multiclass over the model hypotheses, temperature-scaled
(docs/13 §1, §4, FR-SNS-03).

    p_model(h | x) = softmax(z(x) / T)_h

``z`` is the booster's raw multiclass score (the logits), and ``T > 0`` is a single temperature
fitted on the validation split's negative log-likelihood. The probabilities sum to 1.

Training uses **only the labels the bank has observed** by the training cutoff (docs/14 §7),
never `sim_truth`, and only payments in the matured window, split by time into train,
validation and test (``matured_split``). The report holds the test split's per-class
one-vs-rest AUC, macro AUC, multiclass log loss and expected calibration error. The gate
(``gate``) is enforced on `demo` and `eval` only.

Choosing the rows from the database and storing the artifact (``model_artifacts``,
docs/13 §7) belong to the ``TRAIN_SENSING`` job (#88), as for the anomaly detector.
"""

from __future__ import annotations

import io
import itertools
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize_scalar
from sklearn.metrics import log_loss, roc_auc_score

from delphi.sensing.config import PriorConfig, PriorGate, SplitParts

FloatMatrix = NDArray[np.float64]
IntArray = NDArray[np.int64]
_BLOB_FORMAT = 1
_DAY_US = 86_400_000_000
_MATRIX_NDIM = 2
# Split codes returned by ``matured_split``.
OUTSIDE, TRAIN, VALIDATION, TEST = -1, 0, 1, 2


class PriorInputError(ValueError):
    """Training or scoring input the prior model cannot use."""


# ----------------------------------------------------------------------------- training rows


@dataclass(frozen=True, slots=True)
class LabelObservation:
    """A label the bank observed about a payment (docs/05 §8)."""

    payment_id: str
    hypothesis_key: str
    label_source: str
    known_at_us: int


def observed_labels(
    observations: Iterable[LabelObservation], *, cutoff_us: int, precedence: Sequence[str]
) -> dict[str, str]:
    """Each payment's label as the bank knew it at ``cutoff_us``: the labels known by then, the
    highest-precedence source winning (docs/05 §8). Payments with none are left out."""
    rank = {source: i for i, source in enumerate(precedence)}
    best: dict[str, tuple[int, str]] = {}
    for o in observations:
        if o.known_at_us > cutoff_us:
            continue
        if o.label_source not in rank:
            raise PriorInputError(f"label source {o.label_source!r} has no precedence")
        r = rank[o.label_source]
        if o.payment_id not in best or r < best[o.payment_id][0]:
            best[o.payment_id] = (r, o.hypothesis_key)
    return {payment: key for payment, (_, key) in best.items()}


@dataclass(frozen=True, slots=True)
class SplitDays:
    """The matured window's whole days, from the history start: train, validation, test."""

    train: int
    validation: int
    test: int


def split_days(window_days: int, parts: SplitParts) -> SplitDays:
    """docs/13 §4: validation and test get their share of whole days, rounded down; train gets
    the remainder (90 days → 60/15/15, 14 days → 10/2/2)."""
    total = parts.train + parts.validation + parts.test
    validation = window_days * parts.validation // total
    test = window_days * parts.test // total
    return SplitDays(window_days - validation - test, validation, test)


def matured_split(
    initiated_at_us: IntArray,
    *,
    history_start_us: int,
    cutoff_us: int,
    maturity_days: int,
    parts: SplitParts,
) -> NDArray[np.int8]:
    """Each payment's split (``TRAIN``, ``VALIDATION``, ``TEST`` or ``OUTSIDE``).

    The matured window is ``[history start, cutoff − maturity]`` in whole days; payments after
    it are labelled almost only when they are fraud, so they would bias the classes.
    """
    window_days = max((cutoff_us - maturity_days * _DAY_US - history_start_us) // _DAY_US, 0)
    days = split_days(window_days, parts)
    at = np.asarray(initiated_at_us, dtype=np.int64)
    edges = history_start_us + _DAY_US * np.array(
        [0, days.train, days.train + days.validation, window_days], dtype=np.int64
    )
    result = np.full(at.shape, OUTSIDE, dtype=np.int8)
    for code, (start, end) in enumerate(itertools.pairwise(edges)):
        result[(at >= start) & (at < end)] = code
    return result


def encode_labels(
    labels: Sequence[str], classes: Sequence[str]
) -> tuple[IntArray, NDArray[np.bool_]]:
    """Class indices, and which labels are model classes. Others (``FRAUD_OTHER``, a typology
    not yet promoted to a class) are left out of training."""
    index = {c: i for i, c in enumerate(classes)}
    known = np.array([label in index for label in labels], dtype=np.bool_)
    codes = np.array([index.get(label, -1) for label in labels], dtype=np.int64)
    return codes, known


# ----------------------------------------------------------------------------- the model


def feature_matrix(rows: Sequence[Mapping[str, float]], feature_list: Sequence[str]) -> FloatMatrix:
    """Rows of feature dicts → a float64 matrix whose columns follow ``feature_list``."""
    expected = set(feature_list)
    matrix = np.empty((len(rows), len(feature_list)), dtype=np.float64)
    for i, row in enumerate(rows):
        if set(row) != expected:
            missing = sorted(expected - set(row))
            extra = sorted(set(row) - expected)
            raise PriorInputError(f"feature mismatch: missing {missing}, unexpected {extra}")
        matrix[i] = [row[name] for name in feature_list]
    _check_finite(matrix)
    return matrix


def _check_finite(matrix: FloatMatrix) -> None:
    if not np.all(np.isfinite(matrix)):
        raise PriorInputError("features must be finite")


def _softmax(logits: FloatMatrix) -> FloatMatrix:
    shifted = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(shifted)
    result: FloatMatrix = e / e.sum(axis=1, keepdims=True)
    return result


@dataclass(frozen=True, slots=True)
class Contribution:
    """One feature's share of a class's calibrated logit (LightGBM ``pred_contrib`` / T)."""

    feature: str
    value: float
    contribution: float


@dataclass(frozen=True, slots=True, eq=False)
class PriorModel:
    booster: lgb.Booster
    classes: tuple[str, ...]
    feature_list: tuple[str, ...]
    temperature: float
    top_contributions: int
    report: Mapping[str, Any] = field(default_factory=dict)

    def logits(self, matrix: FloatMatrix) -> FloatMatrix:
        """The calibrated logits ``z / T``."""
        raw = np.asarray(self.booster.predict(matrix, raw_score=True), dtype=np.float64)
        return raw.reshape(len(matrix), len(self.classes)) / self.temperature

    def probabilities(self, matrix: FloatMatrix) -> FloatMatrix:
        """``p_model`` for each row; each row sums to 1."""
        _check_finite(matrix)
        return _softmax(self.logits(matrix))

    def predict(self, features: Mapping[str, float]) -> dict[str, float]:
        """``p_model(h)`` for one payment, as the Guardian prior reads it (docs/13 §1)."""
        [row] = self.probabilities(feature_matrix([features], self.feature_list))
        return dict(zip(self.classes, (float(p) for p in row), strict=True))

    def contributions(
        self, features: Mapping[str, float], hypothesis: str | None = None
    ) -> tuple[Contribution, ...]:
        """The top features behind one class's calibrated logit, largest |contribution| first
        (ties by feature order). ``hypothesis`` defaults to the most probable class."""
        matrix = feature_matrix([features], self.feature_list)
        k = len(self.feature_list) + 1  # each class's block ends with its bias term
        contrib = np.asarray(self.booster.predict(matrix, pred_contrib=True), dtype=np.float64)
        if hypothesis is None:
            target = int(np.argmax(self.logits(matrix)[0]))
        elif hypothesis in self.classes:
            target = self.classes.index(hypothesis)
        else:
            raise PriorInputError(f"{hypothesis!r} is not a model class")
        block = contrib[0, target * k : target * k + k - 1] / self.temperature
        order = sorted(range(len(block)), key=lambda i: (-abs(block[i]), i))
        return tuple(
            Contribution(self.feature_list[i], float(matrix[0, i]), float(block[i]))
            for i in order[: self.top_contributions]
        )

    def to_bytes(self) -> bytes:
        """The artifact blob (joblib). Load it only from DELPHI's own ``model_artifacts``."""
        buffer = io.BytesIO()
        joblib.dump(
            {
                "format": _BLOB_FORMAT,
                "booster": self.booster.model_to_string(),
                "classes": list(self.classes),
                "feature_list": list(self.feature_list),
                "temperature": self.temperature,
                "top_contributions": self.top_contributions,
                "report": dict(self.report),
            },
            buffer,
        )
        return buffer.getvalue()

    @classmethod
    def from_bytes(cls, blob: bytes) -> PriorModel:
        payload: dict[str, Any] = joblib.load(io.BytesIO(blob))
        if payload.get("format") != _BLOB_FORMAT:
            raise ValueError(f"unknown prior artifact format {payload.get('format')!r}")
        return cls(
            booster=lgb.Booster(model_str=payload["booster"]),
            classes=tuple(payload["classes"]),
            feature_list=tuple(payload["feature_list"]),
            temperature=float(payload["temperature"]),
            top_contributions=int(payload["top_contributions"]),
            report=payload["report"],
        )


# ----------------------------------------------------------------------------- metrics


def expected_calibration_error(probabilities: FloatMatrix, labels: IntArray, bins: int) -> float:
    """ECE over ``bins`` equal-width bins of the max probability (docs/13 §4)."""
    confidence = probabilities.max(axis=1)
    correct = (probabilities.argmax(axis=1) == labels).astype(np.float64)
    # A confidence of exactly 1 falls in the last bin.
    which = np.minimum((confidence * bins).astype(np.int64), bins - 1)
    total = 0.0
    for b in range(bins):
        mask = which == b
        if mask.any():
            total += mask.sum() * abs(correct[mask].mean() - confidence[mask].mean())
    return float(total / len(labels))


def class_reliability(
    probabilities: FloatMatrix, labels: IntArray, classes: Sequence[str], ece_bins: int
) -> dict[str, dict[str, float]]:
    """Per class: the mean predicted probability against the observed share (calibration in
    the large), and a one-vs-rest ECE over ``ece_bins`` bins of that class's probability.

    The max-probability ECE is dominated by GENUINE; this shows whether the fraud classes are
    calibrated too. Inverse-frequency weights shift the class priors, and a single temperature
    cannot undo that, so the fraud classes may read high here.
    """
    result: dict[str, dict[str, float]] = {}
    for i, c in enumerate(classes):
        p = probabilities[:, i]
        hit = (labels == i).astype(np.float64)
        which = np.minimum((p * ece_bins).astype(np.int64), ece_bins - 1)
        ece = sum(
            float(mask.sum() * abs(hit[mask].mean() - p[mask].mean()))
            for mask in (which == b for b in range(ece_bins))
            if mask.any()
        )
        result[c] = {
            "mean_predicted": float(p.mean()),
            "observed_share": float(hit.mean()),
            "ece": ece / len(labels),
        }
    return result


def split_metrics(
    probabilities: FloatMatrix, labels: IntArray, classes: Sequence[str], ece_bins: int
) -> dict[str, Any]:
    """Per-class one-vs-rest AUC (``None`` when the split lacks positives or negatives), macro
    AUC over the classes that have one, multiclass log loss, ECE, and per-class reliability."""
    auc: dict[str, float | None] = {}
    for i, c in enumerate(classes):
        positives = labels == i
        auc[c] = (
            float(roc_auc_score(positives, probabilities[:, i]))
            if 0 < positives.sum() < len(labels)
            else None
        )
    defined = [a for a in auc.values() if a is not None]
    return {
        "n": len(labels),
        "class_counts": {c: int((labels == i).sum()) for i, c in enumerate(classes)},
        "auc": auc,
        "macro_auc": float(np.mean(defined)) if defined else None,
        "log_loss": float(log_loss(labels, probabilities, labels=list(range(len(classes))))),
        "ece": expected_calibration_error(probabilities, labels, ece_bins),
        "class_reliability": class_reliability(probabilities, labels, classes, ece_bins),
    }


@dataclass(frozen=True, slots=True)
class GateResult:
    enforced: bool
    passed: bool
    reasons: tuple[str, ...]


def gate(test_metrics: Mapping[str, Any] | None, profile: str, config: PriorGate) -> GateResult:
    """docs/13 §4: ECE ≤ max_ece and macro AUC ≥ min_macro_auc on the test split. Enforced only
    on ``config.enforced_profiles``; elsewhere the result is reported and ``passed`` is true."""
    reasons: list[str] = []
    if test_metrics is None:
        reasons.append("no test split")
    else:
        ece, auc = test_metrics["ece"], test_metrics["macro_auc"]
        if ece > config.max_ece:
            reasons.append(f"ECE {ece:.4f} > {config.max_ece}")
        if auc is None:
            reasons.append("macro AUC undefined")
        elif auc < config.min_macro_auc:
            reasons.append(f"macro AUC {auc:.4f} < {config.min_macro_auc}")
    enforced = profile in config.enforced_profiles
    return GateResult(enforced, not reasons or not enforced, tuple(reasons))


# ----------------------------------------------------------------------------- training


@dataclass(frozen=True, slots=True)
class Split:
    """One split's feature matrix (columns follow the feature list) and class indices."""

    features: FloatMatrix
    labels: IntArray

    def __post_init__(self) -> None:
        if self.features.ndim != _MATRIX_NDIM or len(self.features) != len(self.labels):
            raise PriorInputError("a split needs one label per feature row")
        _check_finite(self.features)

    def __len__(self) -> int:
        return len(self.labels)


def class_weights(labels: IntArray, n_classes: int) -> FloatMatrix:
    """Balanced by inverse frequency: ``n / (K_present · n_c)`` per row (docs/13 §4)."""
    counts = np.bincount(labels, minlength=n_classes).astype(np.float64)
    present = int((counts > 0).sum())
    per_class = np.divide(
        len(labels), present * counts, out=np.zeros_like(counts), where=counts > 0
    )
    result: FloatMatrix = per_class[labels]
    return result


def fit_temperature(logits: FloatMatrix, labels: IntArray, bounds: tuple[float, float]) -> float:
    """The T > 0 minimising the negative log-likelihood of ``softmax(logits / T)``."""
    rows = np.arange(len(labels))

    def nll(log_t: float) -> float:
        scaled = logits / math.exp(log_t)
        shifted = scaled - scaled.max(axis=1, keepdims=True)
        log_norm = np.log(np.exp(shifted).sum(axis=1))
        return float(np.mean(log_norm - shifted[rows, labels]))

    low, high = (math.log(b) for b in bounds)
    found = minimize_scalar(nll, bounds=(low, high), method="bounded")
    return math.exp(float(found.x))


def train_prior(
    train: Split,
    validation: Split,
    test: Split,
    feature_list: Sequence[str],
    config: PriorConfig,
    *,
    profile: str,
) -> tuple[PriorModel, GateResult]:
    """Fit the booster on ``train`` (early stopping on ``validation``), the temperature on
    ``validation``, and report on ``test``. Deterministic for a fixed ``config.seed``.

    Without validation rows there is no early stopping and T = 1; without test rows the gate
    fails where it is enforced.
    """
    n_classes = len(config.classes)
    if not feature_list or len(set(feature_list)) != len(feature_list):
        raise PriorInputError("the feature list is empty or has duplicates")
    if len(train) == 0:
        raise PriorInputError("no training payments")
    for split in (train, validation, test):
        if split.features.shape[1] != len(feature_list):
            raise PriorInputError("a split's columns do not match the feature list")
        if len(split) and (split.labels.min() < 0 or split.labels.max() >= n_classes):
            raise PriorInputError("labels must be class indices")
    params = {
        "objective": "multiclass",
        "num_class": n_classes,
        "num_leaves": config.num_leaves,
        "learning_rate": config.learning_rate,
        "seed": config.seed,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 1,
        "verbosity": -1,
    }
    names = list(feature_list)
    data = lgb.Dataset(
        train.features,
        label=train.labels,
        weight=class_weights(train.labels, n_classes),
        feature_name=names,
        free_raw_data=False,
    )
    valid_sets: list[lgb.Dataset] = []
    callbacks: list[Callable[..., Any]] = []
    if len(validation):
        valid_sets.append(lgb.Dataset(validation.features, label=validation.labels, reference=data))
        callbacks.append(lgb.early_stopping(config.early_stopping_rounds, verbose=False))
    booster = lgb.train(
        params,
        data,
        num_boost_round=config.n_estimators,
        valid_sets=valid_sets,
        callbacks=callbacks,
    )
    if booster.best_iteration > 0:
        booster = lgb.Booster(
            model_str=booster.model_to_string(num_iteration=booster.best_iteration)
        )
    model = PriorModel(
        booster, tuple(config.classes), tuple(feature_list), 1.0, config.top_contributions
    )
    temperature = (
        fit_temperature(
            model.logits(validation.features), validation.labels, config.temperature_bounds
        )
        if len(validation)
        else 1.0
    )
    model = PriorModel(
        booster, tuple(config.classes), tuple(feature_list), temperature, config.top_contributions
    )
    test_metrics = (
        split_metrics(
            model.probabilities(test.features), test.labels, config.classes, config.ece_bins
        )
        if len(test)
        else None
    )
    result = gate(test_metrics, profile, config.gate)
    report = {
        "profile": profile,
        "n_train": len(train),
        "n_validation": len(validation),
        "n_test": len(test),
        "train_class_counts": {
            c: int(n)
            for c, n in zip(
                config.classes, np.bincount(train.labels, minlength=n_classes), strict=True
            )
        },
        "boosting_rounds": booster.num_trees() // n_classes,
        "temperature": temperature,
        "test": test_metrics,
        "gate": {
            "enforced": result.enforced,
            "passed": result.passed,
            "reasons": list(result.reasons),
        },
    }
    model = PriorModel(
        booster,
        tuple(config.classes),
        tuple(feature_list),
        temperature,
        config.top_contributions,
        report,
    )
    return model, result
