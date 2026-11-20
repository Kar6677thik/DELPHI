"""The mule scorer model (docs/13 §6, FR-SNS-05): training, calibration, gate and blob."""

from __future__ import annotations

import numpy as np
import pytest

from delphi.sensing.config import MuleConfig, MuleGate, load_sensing_config
from delphi.sensing.features import ACCOUNT_FEATURES
from delphi.sensing.mule import (
    MuleGateResult,
    MuleInputError,
    MuleModel,
    MuleSplit,
    auc,
    mule_gate,
    train_mule,
)

FEATURES = ACCOUNT_FEATURES
N = 600


def _split(n: int, seed: int, mule_share: float = 0.1) -> MuleSplit:
    """Synthetic accounts: mules get many distinct payers, fast dwell and high pass-through."""
    rng = np.random.default_rng(seed)
    labels = (rng.random(n) < mule_share).astype(np.int64)
    m = labels.astype(bool)
    x = np.zeros((n, len(FEATURES)))
    x[:, 0] = np.where(m, rng.poisson(8, n), rng.poisson(1, n))  # inbound_distinct_payers_48h
    x[:, 1] = np.where(m, rng.poisson(15, n), rng.poisson(3, n))  # inbound_count_7d
    x[:, 2] = np.where(m, rng.normal(5.5, 0.4, n), rng.normal(4.0, 0.6, n))  # inbound_sum_7d_log
    x[:, 3] = np.where(m, rng.exponential(30, n), rng.exponential(2000, n))  # dwell minutes
    x[:, 4] = np.where(m, rng.normal(0.95, 0.05, n), rng.normal(0.6, 0.3, n)).clip(0)  # ratio
    x[:, 5] = np.where(m, rng.uniform(1, 60, n), rng.uniform(30, 3650, n))  # account_age_days
    x[:, 6] = np.where(m, rng.random(n) < 0.7, rng.random(n) < 0.1)  # kyc_minimal
    x[:, 7] = np.where(m, rng.poisson(2, n), rng.poisson(0.1, n))  # device_shared_accounts_30d
    x[:, 8] = np.where(m, rng.uniform(0.2, 0.9, n), rng.uniform(0, 0.1, n))  # cash_out_share
    x[:, 9] = 0.0  # complaints_linked: always 0 before a mule's label time
    return MuleSplit(x, labels)


@pytest.fixture(scope="module")
def config() -> MuleConfig:
    return load_sensing_config().mule


@pytest.fixture(scope="module")
def trained(config: MuleConfig) -> tuple[MuleModel, MuleGateResult]:
    return train_mule(
        _split(N, 1), _split(200, 2), _split(200, 3), FEATURES, config, profile="eval"
    )


def test_config_matches_the_spec(config: MuleConfig) -> None:
    # docs/13 §6: LightGBM binary (num_leaves 15, n_estimators 300, seed 7); AUC ≥ 0.90 on eval
    assert (config.num_leaves, config.n_estimators, config.seed) == (15, 300, 7)
    assert config.gate.min_auc == 0.90
    assert config.gate.enforced_profiles == ("eval",)
    assert config.negative_min_age_days == 30
    assert config.label_hypothesis == "MULE"


def test_a_separable_problem_passes_the_enforced_gate(
    trained: tuple[MuleModel, MuleGateResult],
) -> None:
    model, gate = trained
    report = model.report
    assert report["test"]["auc"] > 0.95
    assert report["calibrated"] is True
    assert gate.enforced and gate.passed and not gate.reasons


def test_scores_are_probabilities_and_rank_mules_higher(
    trained: tuple[MuleModel, MuleGateResult],
) -> None:
    model, _ = trained
    split = _split(300, 4)
    scores = model.scores(split.features)
    assert scores.shape == (300,)
    assert np.all((scores >= 0) & (scores <= 1))
    assert scores[split.labels == 1].mean() > scores[split.labels == 0].mean()


def test_p_mule_for_one_account_matches_the_matrix(
    trained: tuple[MuleModel, MuleGateResult],
) -> None:
    model, _ = trained
    split = _split(5, 5)
    row = dict(zip(FEATURES, split.features[0], strict=True))
    assert model.p_mule(row) == pytest.approx(float(model.scores(split.features[:1])[0]))
    with pytest.raises(MuleInputError, match="mismatch"):
        model.p_mule({"inbound_count_7d": 1.0})


def test_training_is_deterministic_and_the_blob_round_trips(config: MuleConfig) -> None:
    args = (_split(N, 1), _split(200, 2), _split(200, 3), FEATURES, config)
    first, _ = train_mule(*args, profile="test")
    second, _ = train_mule(*args, profile="test")
    assert first.to_bytes() == second.to_bytes()
    restored = MuleModel.from_bytes(first.to_bytes())
    probe = _split(50, 6).features
    np.testing.assert_array_equal(restored.scores(probe), first.scores(probe))
    assert restored.feature_list == FEATURES
    assert restored.report == first.report


def test_without_a_two_class_validation_split_scores_stay_uncalibrated(config: MuleConfig) -> None:
    no_mules = MuleSplit(_split(50, 7).features, np.zeros(50, dtype=np.int64))
    model, _ = train_mule(_split(N, 1), no_mules, _split(200, 3), FEATURES, config, profile="test")
    assert model.calibration is None and model.report["calibrated"] is False


# ---------------------------------------------------------------- the gate


@pytest.mark.parametrize(
    ("test_auc", "profile", "passed", "enforced"),
    [
        (0.95, "eval", True, True),
        (0.85, "eval", False, True),
        (None, "eval", False, True),
        (0.50, "test", True, False),  # reported only
        (0.50, "demo", True, False),
    ],
)
def test_gate(test_auc: float | None, profile: str, passed: bool, enforced: bool) -> None:
    result = mule_gate(test_auc, profile, MuleGate(min_auc=0.90, enforced_profiles=("eval",)))
    assert (result.passed, result.enforced) == (passed, enforced)
    assert bool(result.reasons) == (test_auc is None or test_auc < 0.90)


def test_auc_is_undefined_without_both_classes() -> None:
    assert auc(np.array([0.1, 0.9]), np.array([0, 0])) is None
    assert auc(np.array([0.1, 0.9]), np.array([0, 1])) == 1.0


# ---------------------------------------------------------------- bad input


def test_training_needs_mules_and_non_mules(config: MuleConfig) -> None:
    all_clean = MuleSplit(_split(50, 8).features, np.zeros(50, dtype=np.int64))
    with pytest.raises(MuleInputError, match="mules and non-mules"):
        train_mule(all_clean, all_clean, all_clean, FEATURES, config, profile="test")


@pytest.mark.parametrize(
    ("features", "labels", "message"),
    [
        (np.zeros((3, 2)), np.array([0, 1]), "one label per feature row"),
        (np.zeros(3), np.array([0, 1, 0]), "one label per feature row"),
        (np.zeros((2, 2)), np.array([0, 2]), "0 \\(not mule\\) or 1"),
        (np.full((2, 2), np.nan), np.array([0, 1]), "finite"),
    ],
)
def test_bad_splits_are_refused(features: np.ndarray, labels: np.ndarray, message: str) -> None:
    with pytest.raises(MuleInputError, match=message):
        MuleSplit(features, labels.astype(np.int64))


def test_bad_feature_lists_and_matrices_are_refused(
    config: MuleConfig, trained: tuple[MuleModel, MuleGateResult]
) -> None:
    split = _split(N, 1)
    with pytest.raises(MuleInputError, match="empty or has duplicates"):
        train_mule(split, split, split, (), config, profile="test")
    with pytest.raises(MuleInputError, match="do not match"):
        train_mule(split, split, split, FEATURES[:3], config, profile="test")
    model, _ = trained
    with pytest.raises(MuleInputError, match="one column per feature"):
        model.scores(np.zeros((2, 3)))
    with pytest.raises(MuleInputError, match="finite"):
        model.scores(np.full((1, len(FEATURES)), np.inf))


def test_the_report_carries_feature_importance(trained: tuple[MuleModel, MuleGateResult]) -> None:
    model, _ = trained
    gains = model.report["feature_importance_gain"]
    assert set(gains) == set(FEATURES)
    assert all(g >= 0 for g in gains.values()) and sum(gains.values()) > 0
