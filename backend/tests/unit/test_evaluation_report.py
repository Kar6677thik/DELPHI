"""The evaluation report and its target statements (docs/22 §5–6, #124 part 3)."""

from __future__ import annotations

import math
from datetime import date

import pytest

from delphi.evaluation.config import load_evaluation_config
from delphi.evaluation.metrics import Estimate
from delphi.evaluation.report import (
    Kind,
    Reproducibility,
    Verdict,
    assess_targets,
    cell,
    fmt,
    render_report,
)

TARGETS = load_evaluation_config().targets
REPRO = Reproducibility("ds-1", "16", {"eval": 7, "bootstrap": 7}, "v1", "abc123", "deadbeef")


def e(value: float | None, low: float | None = None, high: float | None = None) -> Estimate:
    return Estimate(value, value if low is None else low, value if high is None else high)


def results(**delphi: Estimate) -> dict[str, dict[str, Estimate]]:
    base = {
        "B1": {
            "genuine_hold_rate": e(0.04),
            "fraud_prevented_paise": e(1_000_000),
            "hold_resolution_minutes_median": e(600.0),
            "label_coverage_held": e(0.25),
            "coercion_catch_rate": e(0.1),
        },
        "B2": {"coercion_catch_rate": e(0.2), "genuine_hold_rate": e(0.03)},
        "DELPHI": {
            "genuine_hold_rate": e(0.01),
            "fraud_prevented_paise": e(1_500_000),
            "hold_resolution_minutes_median": e(20.0),
            "label_coverage_held": e(0.9),
            "coercion_catch_rate": e(0.6),
            "dd_pack_completeness": e(1.0),
            "info_yield_bits[CALLER_CHECK]": e(0.4, 0.3, 0.5),
        },
    }
    base["DELPHI"].update(delphi)
    return base


def verdicts(r: dict[str, dict[str, Estimate]]) -> dict[str, Verdict]:
    return {t.target: t.verdict for t in assess_targets(r, TARGETS)}


def test_every_target_met_except_the_unmeasured_golden_hour() -> None:
    assert verdicts(results()) == {
        "Genuine-hold rate": Verdict.MET,
        "Coercion catch rate": Verdict.MET,
        "Median hold resolution time": Verdict.MET,
        "Label coverage (held)": Verdict.MET,
        "Golden-hour recovery ratio": Verdict.NOT_MEASURED,
        "Due-diligence completeness": Verdict.MET,
    }


@pytest.mark.parametrize(
    ("change", "target"),
    [
        ({"genuine_hold_rate": e(0.03)}, "Genuine-hold rate"),  # only 25% lower
        ({"fraud_prevented_paise": e(900_000)}, "Genuine-hold rate"),  # holds less, stops less
        ({"coercion_catch_rate": e(0.3)}, "Coercion catch rate"),  # 1.5× B2
        ({"hold_resolution_minutes_median": e(61.0)}, "Median hold resolution time"),
        ({"hold_resolution_minutes_median": e(math.inf)}, "Median hold resolution time"),
        ({"label_coverage_held": e(0.79)}, "Label coverage (held)"),
        ({"dd_pack_completeness": e(0.99)}, "Due-diligence completeness"),
    ],
)
def test_a_missed_target_says_so(change: dict[str, Estimate], target: str) -> None:
    assessed = {t.target: t for t in assess_targets(results(**change), TARGETS)}
    assert assessed[target].verdict is Verdict.MISSED
    assert assessed[target].statement.startswith("Missed:")


def test_the_missed_genuine_hold_statement_names_each_failing_part() -> None:
    r = results(genuine_hold_rate=e(0.03), fraud_prevented_paise=e(900_000))
    statement = {t.target: t.statement for t in assess_targets(r, TARGETS)}["Genuine-hold rate"]
    assert "not 50% lower" in statement and "prevents less fraud than B1" in statement


def test_an_undefined_metric_is_not_measured_never_met() -> None:
    r = results(coercion_catch_rate=e(None))
    assessed = {t.target: t for t in assess_targets(r, TARGETS)}
    assert assessed["Coercion catch rate"].verdict is Verdict.NOT_MEASURED
    assert "DELPHI coercion_catch_rate" in assessed["Coercion catch rate"].statement


def test_a_ci_across_the_threshold_is_called_inconclusive() -> None:
    r = results(label_coverage_held=e(0.82, 0.74, 0.88))
    label = {t.target: t for t in assess_targets(r, TARGETS)}["Label coverage (held)"]
    assert label.verdict is Verdict.MET and "not conclusive" in label.statement


def test_b1_never_resolving_makes_any_finite_delphi_median_a_win() -> None:
    r = results()
    r["B1"]["hold_resolution_minutes_median"] = e(math.inf)
    assert verdicts(r)["Median hold resolution time"] is Verdict.MET


def test_formatting() -> None:
    assert fmt(1_234_500, Kind.RUPEES) == "₹12,345"
    assert fmt(0.1234, Kind.SHARE) == "12.3%"
    assert fmt(math.inf, Kind.MINUTES) == "∞"
    assert fmt(None, Kind.NUMBER) == "n/a"
    assert cell(e(0.5, 0.4, 0.6), Kind.SHARE) == "50.0% [40.0%, 60.0%]"
    assert cell(Estimate(0.5, None, None), Kind.SHARE) == "50.0%"


def test_the_report_has_every_section() -> None:
    r = results()
    r["DELPHI-myopic"] = {"genuine_hold_rate": e(0.02)}
    text = render_report(r, targets=TARGETS, reproducibility=REPRO, on=date(2026, 11, 22))
    for heading in (
        "# Evaluation report, 2026-11-22",
        "## Results",
        "### Ablations",
        "### Information yield per probe (DELPHI)",
        "## Pre-registered targets (docs/22 §5)",
        "## Caveats",
        "## Reproducibility",
    ):
        assert heading in text
    assert "| Metric | B1 | B2 | DELPHI |" in text
    assert "| CALLER_CHECK | 0.4 [0.3, 0.5] |" in text
    assert "Golden-hour recovery ratio | ≥ 2× the manual baseline | Not measured" in text
    assert "- Seeds: bootstrap 7, eval 7" in text and "`deadbeef`" in text
    assert "DELPHI-no-active" not in text  # an ablation that was not run is left out
