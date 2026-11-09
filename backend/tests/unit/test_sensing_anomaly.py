"""The anomaly percentile q (docs/13 §5, FR-SNS-04)."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from delphi.sensing.anomaly import (
    AnomalyInputError,
    AnomalyModel,
    feature_matrix,
    train_anomaly,
)
from delphi.sensing.config import AnomalyConfig, load_sensing_config

FEATURES = ("amount_log10", "hour_deviation", "count_24h")
N_TRAIN = 400


def _rows(n: int, seed: int) -> list[dict[str, float]]:
    rng = np.random.default_rng(seed)
    return [
        {
            "amount_log10": float(rng.normal(3.0, 0.4)),
            "hour_deviation": float(abs(rng.normal(0.0, 1.5))),
            "count_24h": float(rng.poisson(2)),
        }
        for _ in range(n)
    ]


@pytest.fixture(scope="module")
def config() -> AnomalyConfig:
    return load_sensing_config().anomaly


@pytest.fixture(scope="module")
def model(config: AnomalyConfig) -> AnomalyModel:
    return train_anomaly(_rows(N_TRAIN, seed=1), FEATURES, config)


def test_config_matches_the_spec(config: AnomalyConfig) -> None:
    # docs/13 §5: IsolationForest(n_estimators=200, contamination="auto", random_state=7),
    # with 10,001 quantiles of the training scores.
    assert config.n_estimators == 200
    assert config.contamination == "auto"
    assert config.random_state == 7
    assert config.n_quantiles == 10_001


def test_quantile_table_is_sorted_and_full_size(model: AnomalyModel, config: AnomalyConfig) -> None:
    assert model.quantiles.shape == (config.n_quantiles,)
    assert np.all(np.diff(model.quantiles) >= 0)


@settings(max_examples=500, deadline=None)
@given(
    a=st.floats(min_value=-2.0, max_value=2.0, allow_nan=False),
    b=st.floats(min_value=-2.0, max_value=2.0, allow_nan=False),
)
def test_q_is_monotone_in_the_score_and_in_unit_interval(
    model: AnomalyModel, a: float, b: float
) -> None:
    low, high = sorted((a, b))
    q_low, q_high = model.percentile_of_score(low), model.percentile_of_score(high)
    assert 0.0 <= q_low <= q_high <= 1.0


def test_q_extremes(model: AnomalyModel) -> None:
    assert model.percentile_of_score(float(model.quantiles[0]) - 1.0) == 0.0
    assert model.percentile_of_score(float(model.quantiles[-1]) + 1.0) == 1.0


def test_q_of_training_scores_is_roughly_uniform(model: AnomalyModel) -> None:
    rows = _rows(N_TRAIN, seed=1)
    qs = np.array([model.percentile(row) for row in rows])
    assert np.all((qs >= 0.0) & (qs <= 1.0))
    assert abs(float(np.median(qs)) - 0.5) < 0.05


def test_an_outlier_is_anomalous_and_a_typical_payment_is_not(model: AnomalyModel) -> None:
    typical = {"amount_log10": 3.0, "hour_deviation": 0.5, "count_24h": 2.0}
    outlier = {"amount_log10": 6.5, "hour_deviation": 11.0, "count_24h": 30.0}
    assert model.percentile(outlier) > 0.99
    assert model.percentile(typical) < 0.9
    assert model.score(outlier) > model.score(typical)


def test_training_is_deterministic_for_a_fixed_seed(config: AnomalyConfig) -> None:
    rows = _rows(N_TRAIN, seed=2)
    first, second = train_anomaly(rows, FEATURES, config), train_anomaly(rows, FEATURES, config)
    np.testing.assert_array_equal(first.quantiles, second.quantiles)
    probe = {"amount_log10": 4.2, "hour_deviation": 3.0, "count_24h": 5.0}
    assert first.percentile(probe) == second.percentile(probe)
    assert hashlib.sha256(first.to_bytes()).digest() == hashlib.sha256(second.to_bytes()).digest()


def test_blob_round_trip_gives_the_same_q(model: AnomalyModel) -> None:
    restored = AnomalyModel.from_bytes(model.to_bytes())
    assert restored.feature_list == model.feature_list
    assert restored.n_train == N_TRAIN
    for row in _rows(50, seed=3):
        assert restored.percentile(row) == model.percentile(row)


def test_unknown_blob_format_is_refused(model: AnomalyModel) -> None:
    import io

    import joblib

    buffer = io.BytesIO()
    joblib.dump({"format": 999}, buffer)
    with pytest.raises(ValueError, match="format"):
        AnomalyModel.from_bytes(buffer.getvalue())


def test_feature_order_comes_from_the_feature_list(model: AnomalyModel) -> None:
    row = {"count_24h": 2.0, "amount_log10": 3.0, "hour_deviation": 0.5}
    assert model.percentile(row) == model.percentile(dict(reversed(list(row.items()))))


def test_metrics(model: AnomalyModel) -> None:
    metrics = model.metrics()
    assert metrics["n_train"] == N_TRAIN
    assert metrics["score_p50"] <= metrics["score_p90"] <= metrics["score_p99"]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"amount_log10": 3.0, "hour_deviation": 0.5}, "missing"),
        ({"amount_log10": 3.0, "hour_deviation": 0.5, "count_24h": 1.0, "x": 0.0}, "unexpected"),
        ({"amount_log10": float("nan"), "hour_deviation": 0.5, "count_24h": 1.0}, "finite"),
    ],
)
def test_bad_features_are_refused(model: AnomalyModel, row: dict[str, float], message: str) -> None:
    with pytest.raises(AnomalyInputError, match=message):
        model.percentile(row)


@pytest.mark.parametrize(
    ("rows", "features", "message"),
    [
        ([], FEATURES, "no training"),
        (_rows(5, seed=4), (), "empty"),
        (_rows(5, seed=4), ("amount_log10", "amount_log10"), "duplicates"),
    ],
)
def test_bad_training_input_is_refused(
    config: AnomalyConfig,
    rows: list[dict[str, float]],
    features: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(AnomalyInputError, match=message):
        train_anomaly(rows, features, config)


def test_feature_matrix_shape() -> None:
    matrix = feature_matrix(_rows(7, seed=5), FEATURES)
    assert matrix.shape == (7, 3)
    assert matrix.dtype == np.float64
