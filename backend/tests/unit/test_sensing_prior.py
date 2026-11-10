"""The prior model (docs/13 §4, FR-SNS-03) on synthetic data."""

from __future__ import annotations

import math

import numpy as np
import pytest

from delphi.sensing.config import SplitParts, load_sensing_config
from delphi.sensing.prior import (
    OUTSIDE,
    TEST,
    TRAIN,
    VALIDATION,
    LabelObservation,
    PriorInputError,
    PriorModel,
    Split,
    class_weights,
    encode_labels,
    expected_calibration_error,
    feature_matrix,
    fit_temperature,
    gate,
    matured_split,
    observed_labels,
    split_days,
    train_prior,
)

CONFIG = load_sensing_config().prior
DAY = 86_400_000_000
PARTS = SplitParts(train=4, validation=1, test=1)
FEATURES = ("signal", "noise")


def test_the_split_is_two_thirds_a_sixth_and_a_sixth_in_whole_days() -> None:
    assert split_days(90, PARTS) == split_days(90, CONFIG.split_parts)
    assert (split_days(90, PARTS).train, split_days(90, PARTS).validation) == (60, 15)
    days = split_days(14, PARTS)  # `test`: 21 days of history, 7 to mature
    assert (days.train, days.validation, days.test) == (10, 2, 2)
    assert split_days(17, PARTS).train == 13  # 17 // 6 = 2 each; the remainder goes to train


def test_payments_after_the_matured_window_are_left_out() -> None:
    start = 1_000 * DAY
    at = np.array([start - 1, start, start + 10 * DAY - 1, start + 10 * DAY, start + 12 * DAY,
                   start + 14 * DAY - 1, start + 14 * DAY], dtype=np.int64)  # fmt: skip
    split = matured_split(
        at, history_start_us=start, cutoff_us=start + 21 * DAY, maturity_days=7, parts=PARTS
    )
    assert split.tolist() == [OUTSIDE, TRAIN, TRAIN, VALIDATION, TEST, TEST, OUTSIDE]


def test_observed_labels_follow_precedence_and_the_cutoff() -> None:
    labels = observed_labels(
        [
            LabelObservation("p1", "GENUINE", "NO_COMPLAINT_60D", 10),
            LabelObservation("p1", "ATO", "CUSTOMER_COMPLAINT", 20),  # a late complaint wins
            LabelObservation("p2", "APP_INVESTMENT", "CUSTOMER_COMPLAINT", 99),  # after cutoff
            LabelObservation("p3", "GENUINE", "NO_COMPLAINT_60D", 5),
            LabelObservation("p3", "ATO", "INVESTIGATOR_DISPOSITION", 6),
        ],
        cutoff_us=50,
        precedence=CONFIG.label_precedence,
    )
    assert labels == {"p1": "ATO", "p3": "ATO"}
    with pytest.raises(PriorInputError, match="precedence"):
        observed_labels([LabelObservation("p", "ATO", "RUMOUR", 0)], cutoff_us=1, precedence=("A",))


def test_labels_that_are_not_classes_are_left_out() -> None:
    codes, known = encode_labels(["GENUINE", "FRAUD_OTHER", "MULE"], CONFIG.classes)
    assert codes.tolist() == [0, -1, 4] and known.tolist() == [True, False, True]


def test_class_weights_balance_the_present_classes() -> None:
    weights = class_weights(np.array([0, 0, 0, 1], dtype=np.int64), 3)
    assert weights.tolist() == pytest.approx([4 / 6, 4 / 6, 4 / 6, 2.0])
    assert weights.sum() == pytest.approx(4)


def test_ece_of_a_perfectly_calibrated_split_is_zero() -> None:
    p = np.array([[0.75, 0.25]] * 4)
    assert expected_calibration_error(p, np.array([0, 0, 0, 1]), 15) == pytest.approx(0)
    overconfident = np.array([[1.0, 0.0]] * 2)  # confidence 1 falls in the last bin
    assert expected_calibration_error(overconfident, np.array([0, 1]), 15) == pytest.approx(0.5)


def test_temperature_undoes_overconfident_logits() -> None:
    rng = np.random.default_rng(1)
    true_logits = rng.normal(size=(4000, 3))
    p = np.exp(true_logits) / np.exp(true_logits).sum(axis=1, keepdims=True)
    labels = np.array([rng.choice(3, p=row) for row in p], dtype=np.int64)
    assert fit_temperature(3.0 * true_logits, labels, CONFIG.temperature_bounds) == pytest.approx(
        3.0, rel=0.1
    )


def test_the_gate_is_enforced_only_on_demo_and_eval() -> None:
    good = {"ece": 0.01, "macro_auc": 0.9}
    bad = {"ece": 0.2, "macro_auc": 0.6}
    assert gate(good, "eval", CONFIG.gate).passed
    failed = gate(bad, "demo", CONFIG.gate)
    assert failed.enforced and not failed.passed and len(failed.reasons) == 2
    reported = gate(bad, "test", CONFIG.gate)
    assert not reported.enforced and reported.passed and reported.reasons
    assert not gate(None, "eval", CONFIG.gate).passed
    assert not gate({"ece": 0.0, "macro_auc": None}, "eval", CONFIG.gate).passed


def _synthetic(n: int, seed: int) -> Split:
    """Two features; the class is mostly given by ``signal`` (three bands)."""
    rng = np.random.default_rng(seed)
    labels = rng.choice(3, size=n, p=[0.8, 0.15, 0.05]).astype(np.int64)
    signal = labels + rng.normal(scale=0.35, size=n)
    return Split(np.column_stack([signal, rng.normal(size=n)]), labels)


def _trained() -> tuple[PriorModel, Split]:
    config = CONFIG.model_copy(update={"classes": ("GENUINE", "ATO", "MULE"), "n_estimators": 60})
    test = _synthetic(600, 3)
    model, result = train_prior(
        _synthetic(1500, 1), _synthetic(500, 2), test, FEATURES, config, profile="eval"
    )
    assert result.enforced and result.passed, result.reasons
    return model, test


def test_a_trained_model_is_calibrated_sums_to_one_and_explains_itself() -> None:
    model, test = _trained()
    report = model.report
    assert report["test"]["macro_auc"] > 0.9 and report["test"]["ece"] < 0.05
    assert report["boosting_rounds"] <= 60 and report["temperature"] > 0
    probabilities = model.probabilities(test.features)
    assert np.allclose(probabilities.sum(axis=1), 1.0)
    p = model.predict({"signal": 2.0, "noise": 0.0})
    assert list(p) == ["GENUINE", "ATO", "MULE"] and math.isclose(sum(p.values()), 1.0)
    assert max(p, key=p.__getitem__) == "MULE"
    top = model.contributions({"signal": 2.0, "noise": 0.0})
    assert top[0].feature == "signal" and top[0].value == 2.0 and top[0].contribution > 0
    assert {c.feature for c in model.contributions({"signal": 0.0, "noise": 0.0}, "ATO")} <= {
        "signal",
        "noise",
    }


def test_training_is_reproducible_and_the_blob_round_trips() -> None:
    first, test = _trained()
    second, _ = _trained()
    assert np.array_equal(first.probabilities(test.features), second.probabilities(test.features))
    again = PriorModel.from_bytes(first.to_bytes())
    assert again.report == first.report and again.temperature == first.temperature
    assert np.array_equal(again.probabilities(test.features), first.probabilities(test.features))
    row = {"signal": 1.0, "noise": 0.5}
    assert again.contributions(row) == first.contributions(row) == second.contributions(row)


def test_bad_inputs_are_refused() -> None:
    model, _ = _trained()
    with pytest.raises(PriorInputError, match="mismatch"):
        model.predict({"signal": 1.0})
    with pytest.raises(PriorInputError, match="finite"):
        feature_matrix([{"signal": math.nan, "noise": 0.0}], FEATURES)
    with pytest.raises(PriorInputError, match="not a model class"):
        model.contributions({"signal": 1.0, "noise": 0.0}, "FRAUD_OTHER")
    with pytest.raises(PriorInputError, match="one label per"):
        Split(np.zeros((2, 2)), np.zeros(3, dtype=np.int64))
    empty = Split(np.zeros((0, 2)), np.zeros(0, dtype=np.int64))
    with pytest.raises(PriorInputError, match="no training"):
        train_prior(empty, empty, empty, FEATURES, CONFIG, profile="test")
    with pytest.raises(PriorInputError, match="class indices"):
        train_prior(
            Split(np.zeros((1, 2)), np.array([9])), empty, empty, FEATURES, CONFIG, profile="test"
        )
    with pytest.raises(ValueError, match="format"):
        PriorModel.from_bytes(_blob({"format": 0}))


def test_without_validation_or_test_rows_t_is_one_and_the_gate_fails_where_enforced() -> None:
    empty = Split(np.zeros((0, 2)), np.zeros(0, dtype=np.int64))
    config = CONFIG.model_copy(update={"classes": ("GENUINE", "ATO", "MULE"), "n_estimators": 5})
    model, result = train_prior(_synthetic(300, 4), empty, empty, FEATURES, config, profile="demo")
    assert model.temperature == 1.0 and model.report["test"] is None
    assert result.enforced and not result.passed


def _blob(payload: dict[str, object]) -> bytes:
    import io

    import joblib

    buffer = io.BytesIO()
    joblib.dump(payload, buffer)
    return buffer.getvalue()
