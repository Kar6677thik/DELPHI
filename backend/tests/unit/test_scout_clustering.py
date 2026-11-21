"""The Scout's clustering core on synthetic clusters (docs/11 §2, FR-SCT-01/02, #117)."""

from __future__ import annotations

import numpy as np
import pytest

from delphi.kb.loader import load_and_validate
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.scout.clustering import (
    NOISE,
    FeatureSpace,
    ScoutItem,
    cluster_labels,
    distinguishing_features,
    draft_likelihoods,
    matrix,
    outcome_profile,
    scout,
    template_name,
)
from tests.conftest import KB_DIR

KB = load_and_validate(KB_DIR, CLASSIFIER_NAMES)
SOURCES = {
    "CALL_STATUS": ("ON_CALL_UNKNOWN_NUMBER", "ON_CALL_KNOWN_CONTACT", "NOT_ON_CALL"),
    "DEVICE_POSTURE": ("REMOTE_ACCESS_ACTIVE", "NEW_DEVICE", "KNOWN_DEVICE"),
    "PURPOSE_QUESTION": (
        "PLAUSIBLE_NORMAL_TIMING",
        "HIGH_RISK_PURPOSE",
        "TIMING_ANOMALOUS",
        "BELIEVES_RECEIVING_MONEY",
    ),
}
SPACE = FeatureSpace(
    sources=SOURCES,
    feature_means={"amount_log10": 3.5, "hour_deviation": 1.5},
    feature_stds={"amount_log10": 0.5, "hour_deviation": 1.0},
    channels=("UPI_PUSH", "IMPS", "UPI_COLLECT_APPROVAL"),
    f3_stages=("INITIAL_ACCESS", "EXECUTION"),
)
RNG = np.random.default_rng(7)


def _items(
    prefix: str, n: int, outcomes: dict[str, str], amount: float, channel: str
) -> list[ScoutItem]:
    return [
        ScoutItem(
            id=f"{prefix}{i}",
            outcomes=outcomes,
            features={
                "amount_log10": amount + RNG.normal(0, 0.05),
                "hour_deviation": 1.0 + RNG.normal(0, 0.1),
            },
            channel=channel,
        )
        for i in range(n)
    ]


# A: remote-access screen-share pattern on large IMPS payments; B: collect-request refunds.
PATTERN_A = _items(
    "a",
    12,
    {"CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER", "DEVICE_POSTURE": "REMOTE_ACCESS_ACTIVE"},
    5.5,
    "IMPS",
)
PATTERN_B = _items(
    "b", 10, {"PURPOSE_QUESTION": "BELIEVES_RECEIVING_MONEY"}, 3.2, "UPI_COLLECT_APPROVAL"
)
REFERENCE = _items(
    "r", 30, {"CALL_STATUS": "NOT_ON_CALL", "DEVICE_POSTURE": "KNOWN_DEVICE"}, 3.5, "UPI_PUSH"
)


def test_kb_v1_holds_the_scout_numbers() -> None:
    """docs/11 §2: HDBSCAN(min_cluster_size=5, min_samples=3), ESS capped at the default 20."""
    assert (KB.config.scout.min_cluster_size, KB.config.scout.min_samples) == (5, 3)
    assert KB.config.elicitation.default_ess == 20


def test_the_matrix_one_hots_outcomes_with_not_observed_and_z_scores_features() -> None:
    item = ScoutItem("x", {"CALL_STATUS": "NOT_ON_CALL"}, {"amount_log10": 4.5}, "IMPS")
    row = dict(zip(SPACE.columns, matrix([item], SPACE)[0], strict=True))
    assert row["CALL_STATUS=NOT_ON_CALL"] == 1 and row["CALL_STATUS=not_observed"] == 0
    assert row["DEVICE_POSTURE=not_observed"] == 1
    assert row["z:amount_log10"] == pytest.approx(2.0)  # (4.5 − 3.5) / 0.5
    assert row["z:hour_deviation"] == 0.0  # a missing feature sits at the mean
    assert row["channel=IMPS"] == 1 and row["f3=EXECUTION"] == 0


def test_two_planted_patterns_become_two_clusters_and_scattered_points_stay_in_the_queue() -> None:
    scattered = [
        ScoutItem(f"n{i}", {}, {"amount_log10": float(v)}, "UPI_PUSH")
        for i, v in enumerate((-10.0, 15.0, 30.0))  # far from both patterns and each other
    ]
    result = scout(
        [*PATTERN_A, *PATTERN_B, *scattered],
        REFERENCE,
        SPACE,
        min_cluster_size=KB.config.scout.min_cluster_size,
        min_samples=KB.config.scout.min_samples,
        max_ess=KB.config.elicitation.default_ess,
    )
    members = sorted(sorted(c.member_ids) for c in result.clusters)
    assert members == [sorted(i.id for i in PATTERN_A), sorted(i.id for i in PATTERN_B)]
    assert set(result.noise_ids) == {"n0", "n1", "n2"}
    a = next(c for c in result.clusters if "a0" in c.member_ids)
    top = [d["feature"] for d in a.distinguishing_features]
    assert len(top) <= 8
    # The planted columns: every A member has them and no reference item does, so they
    # separate the groups perfectly and rank first (traits the cluster has before those the
    # reference has, e.g. NOT_ON_CALL), and they name the cluster.
    planted = {
        "CALL_STATUS=ON_CALL_UNKNOWN_NUMBER",
        "DEVICE_POSTURE=REMOTE_ACCESS_ACTIVE",
        "channel=IMPS",
    }
    assert set(top[:3]) == planted
    assert all(d["separates"] and d["smd"] is None for d in a.distinguishing_features[:3])
    assert all(d["cluster_mean"] == 1.0 for d in a.distinguishing_features[:3])
    assert a.suggested_name.endswith(", ".join(str(f) for f in top[:3]))
    assert a.outcome_profile["DEVICE_POSTURE"]["REMOTE_ACCESS_ACTIVE"] == 1.0
    assert "PURPOSE_QUESTION" not in a.outcome_profile  # never observed by the cluster
    assert a.suggested_name.startswith(f"Candidate pattern {a.number}: ")


def test_draft_likelihood_rows_are_laplace_smoothed_with_a_capped_ess() -> None:
    members = [
        ScoutItem("1", {"CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER"}, {}, "IMPS"),
        ScoutItem("2", {"CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER"}, {}, "IMPS"),
        ScoutItem("3", {"CALL_STATUS": "NOT_ON_CALL"}, {}, "IMPS"),
    ]
    rows = draft_likelihoods(members, SOURCES, max_ess=20)
    # (counts + 1) / (n + |O|) with n = 3, |O| = 3: (2+1)/6, (0+1)/6, (1+1)/6; ESS = min(6, 20).
    assert rows["CALL_STATUS"]["probs"] == pytest.approx([3 / 6, 1 / 6, 2 / 6])
    assert rows["CALL_STATUS"]["ess"] == 6
    assert set(rows) == {"CALL_STATUS"}  # unobserved sources get no row (uniform)
    many = [ScoutItem(str(i), {"CALL_STATUS": "NOT_ON_CALL"}, {}, "IMPS") for i in range(40)]
    assert draft_likelihoods(many, SOURCES, max_ess=20)["CALL_STATUS"]["ess"] == 20
    assert outcome_profile(members, SOURCES)["CALL_STATUS"][
        "ON_CALL_UNKNOWN_NUMBER"
    ] == pytest.approx(2 / 3)


def test_too_few_items_are_all_noise_and_the_name_template_degrades() -> None:
    result = scout(PATTERN_A[:3], REFERENCE, SPACE, min_cluster_size=5, min_samples=3, max_ess=20)
    assert result.clusters == () and len(result.noise_ids) == 3
    assert scout([], REFERENCE, SPACE, min_cluster_size=5, min_samples=3, max_ess=20).clusters == ()
    assert template_name(4, []) == "Candidate pattern 4"
    assert template_name(
        2, [{"feature": "a"}, {"feature": "b"}, {"feature": "c"}, {"feature": "d"}]
    ) == ("Candidate pattern 2: a, b, c")


# Proposed KB value (docs/11 §2, #197): `scout.cluster_selection_epsilon`, pending approval.
PROPOSED_EPSILON = 1.0
SCATTERED = [
    ScoutItem(f"n{i}", {}, {"amount_log10": float(v)}, "UPI_PUSH")
    for i, v in enumerate((-10.0, 15.0, 30.0))
]


def _run(queue: list[ScoutItem], epsilon: float = PROPOSED_EPSILON) -> object:
    return scout(
        queue,
        REFERENCE,
        SPACE,
        min_cluster_size=KB.config.scout.min_cluster_size,
        min_samples=KB.config.scout.min_samples,
        max_ess=KB.config.elicitation.default_ess,
        selection_epsilon=epsilon,
    )


def test_a_queue_holding_one_pattern_is_one_cluster_not_all_noise() -> None:
    """#197 review: the Scout's main case (S-4's collect-refund scams fill the queue)."""
    result = _run([*PATTERN_B, *SCATTERED])
    [cluster] = result.clusters  # type: ignore[attr-defined]
    assert sorted(cluster.member_ids) == sorted(i.id for i in PATTERN_B)
    assert set(result.noise_ids) == {"n0", "n1", "n2"}  # type: ignore[attr-defined]
    [alone] = _run(PATTERN_B).clusters  # type: ignore[attr-defined]
    assert len(alone.member_ids) == len(PATTERN_B)


def test_without_an_epsilon_one_pattern_still_gives_its_dense_core() -> None:
    """With HDBSCAN's default (no epsilon) the single cluster keeps its densest members only:
    one cluster, never zero, which is why the KB value is proposed."""
    [core] = _run(PATTERN_B, epsilon=0.0).clusters  # type: ignore[attr-defined]
    assert KB.config.scout.min_cluster_size <= len(core.member_ids) <= len(PATTERN_B)
    assert set(core.member_ids) <= {i.id for i in PATTERN_B}


def test_identical_items_are_one_cluster() -> None:
    labels = cluster_labels(np.ones((12, 4)), min_cluster_size=5, min_samples=3)
    assert set(labels.tolist()) == {0}


def test_a_pattern_one_answer_apart_stays_its_own_cluster() -> None:
    near = _items("m", 8, {"PURPOSE_QUESTION": "HIGH_RISK_PURPOSE"}, 3.2, "UPI_COLLECT_APPROVAL")
    result = _run([*PATTERN_B, *near])
    members = sorted(sorted(c.member_ids) for c in result.clusters)  # type: ignore[attr-defined]
    assert members == sorted([sorted(i.id for i in PATTERN_B), sorted(i.id for i in near)])
    assert NOISE == -1


COLUMNS = ["flag", "noise", "same", "spread"]


def test_a_column_constant_in_both_groups_at_different_values_ranks_first() -> None:
    """#197 review: the clearest signal (every member has it, no reference item does) must
    not score 0; float noise must not get through as a feature."""
    cluster = np.array([[1.0, 0.3, 1.0, 2.0], [1.0, 0.3, 1.0, 4.0], [1.0, 0.3, 1.0, 6.0]])
    reference = np.array([[0.0, 0.3 + 1e-16, 1.0, 0.0], [0.0, 0.3, 1.0, 1.0], [0.0, 0.3, 1.0, 2.0]])
    found = distinguishing_features(cluster, reference, COLUMNS)
    assert [d["feature"] for d in found] == ["flag", "spread"]  # not "noise", not "same"
    flag = found[0]
    assert flag["separates"] is True and flag["smd"] is None
    assert (flag["cluster_mean"], flag["reference_mean"]) == (1.0, 0.0)
    # spread: diff 3, pooled sqrt((8/3 + 2/3) / 2) = sqrt(5/3)
    assert found[1]["separates"] is False
    assert found[1]["smd"] == pytest.approx(3 / np.sqrt(5 / 3), abs=1e-4)


def test_with_no_reference_items_there_are_no_distinguishing_features() -> None:
    cluster = np.ones((5, len(COLUMNS)))
    assert distinguishing_features(cluster, np.zeros((0, len(COLUMNS))), COLUMNS) == []
    result = scout(
        PATTERN_A, [], SPACE, min_cluster_size=5, min_samples=3, max_ess=20, selection_epsilon=1.0
    )
    [cluster_a] = result.clusters
    assert cluster_a.distinguishing_features == []
    assert cluster_a.suggested_name == f"Candidate pattern {cluster_a.number}"
