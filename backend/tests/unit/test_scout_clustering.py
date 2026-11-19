"""The Scout's clustering core on synthetic clusters (docs/11 §2, FR-SCT-01/02, #117)."""

from __future__ import annotations

import numpy as np
import pytest

from delphi.kb.loader import load_and_validate
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.scout.clustering import (
    FeatureSpace,
    ScoutItem,
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
    assert {"DEVICE_POSTURE=REMOTE_ACCESS_ACTIVE", "z:amount_log10", "channel=IMPS"} & set(top[:4])
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
