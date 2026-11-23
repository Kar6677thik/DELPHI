"""The evaluation report (docs/22 §5–6): results tables, an honest statement for every
pre-registered target, the caveats, and the reproducibility block, as markdown for
``docs/reports/evaluation-<date>.md``.

Every target gets a statement, met or not (docs/22 §5). Verdicts compare point estimates. Where
a single-policy target's 95% CI crosses its threshold, the statement says so. A target whose
metric this run cannot compute says "not measured", never "met".
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from delphi.evaluation.config import TargetsConfig
from delphi.evaluation.metrics import INFO_YIELD, Estimate

B1, B2, DELPHI = "B1", "B2", "DELPHI"
HEADLINE = (B1, B2, DELPHI)
ABLATIONS = ("DELPHI-myopic", "DELPHI-no-active")
_PAISE_PER_RUPEE = 100
_PERCENT = 100

Results = Mapping[str, Mapping[str, Estimate]]  # policy → metric → estimate


class Kind(StrEnum):
    RUPEES = "RUPEES"  # the metric is in paise; shown in ₹
    SHARE = "SHARE"  # shown as a percentage
    MINUTES = "MINUTES"
    NUMBER = "NUMBER"


# docs/22 §4, in its order: (metric, row label, how it is shown)
ROWS: tuple[tuple[str, str, Kind], ...] = (
    ("fraud_prevented_paise", "Fraud ₹ prevented", Kind.RUPEES),
    ("fraud_recall_count", "Fraud recall (count)", Kind.SHARE),
    ("fraud_recall_amount", "Fraud recall (₹)", Kind.SHARE),
    ("coercion_catch_rate", "Coercion catch rate", Kind.SHARE),
    ("genuine_hold_rate", "Genuine-hold rate", Kind.SHARE),
    ("genuine_hold_rate_vulnerable", "Genuine-hold rate (vulnerable)", Kind.SHARE),
    ("hold_resolution_minutes_median", "Median hold resolution time", Kind.MINUTES),
    ("hold_resolution_minutes_p90", "p90 hold resolution time", Kind.MINUTES),
    ("friction_per_fraud_paise_prevented", "Friction cost per fraud ₹ prevented", Kind.NUMBER),
    ("active_probes_per_genuine_payment", "Active probes per genuine payment", Kind.NUMBER),
    ("label_coverage_held", "Label coverage (held, 7 days)", Kind.SHARE),
    (
        "uncertainty_reduced_bits_per_investigator_minute",
        "Uncertainty reduced per investigator minute (bits)",
        Kind.NUMBER,
    ),
    ("dd_pack_completeness", "Due-diligence completeness", Kind.SHARE),
    ("typology_attribution_accuracy", "Typology attribution accuracy", Kind.SHARE),
)
# docs/22 §4 metrics this run does not compute yet, and why.
NOT_MEASURED = (
    ("Golden-hour recovery ratio, tracing delay", "needs the Chase loop (#112, #113)"),
    ("Wrongful liens", "needs the Chase loop (#112, #113)"),
)
CAVEATS = (
    "The data is synthetic, and fraud is oversampled.",
    "The response models are perturbations of KB v1, so the KB is roughly right by "
    "construction. A real deployment must elicit its own numbers.",
    "The engine assumes naive Bayes: observations are independent given the hypothesis.",
    "Unresolved holds count as +∞ in the hold resolution times (docs/22 §4).",
)


class Verdict(StrEnum):
    MET = "Met"
    MISSED = "Missed"
    NOT_MEASURED = "Not measured"


@dataclass(frozen=True, slots=True)
class TargetResult:
    target: str
    threshold: str
    verdict: Verdict
    statement: str


@dataclass(frozen=True, slots=True)
class Reproducibility:
    dataset_id: str
    generator_version: str
    seeds: Mapping[str, int]
    kb_version: str
    kb_sha256: str
    git_sha: str


# --------------------------------------------------------------------------- formatting


def fmt(value: float | None, kind: Kind) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "∞"
    match kind:
        case Kind.RUPEES:
            return f"₹{value / _PAISE_PER_RUPEE:,.0f}"
        case Kind.SHARE:
            return f"{value * _PERCENT:.1f}%"
        case Kind.MINUTES:
            return f"{value:,.1f} min"
        case Kind.NUMBER:
            return f"{value:.3g}"


def cell(estimate: Estimate | None, kind: Kind) -> str:
    if estimate is None or estimate.value is None:
        return "n/a"
    if estimate.low is None or estimate.high is None:
        return fmt(estimate.value, kind)
    return f"{fmt(estimate.value, kind)} [{fmt(estimate.low, kind)}, {fmt(estimate.high, kind)}]"


def _table(results: Results, policies: Sequence[str]) -> list[str]:
    lines = [
        "| Metric | " + " | ".join(policies) + " |",
        "|---|" + "---|" * len(policies),
    ]
    for key, label, kind in ROWS:
        cells = [cell(results.get(p, {}).get(key), kind) for p in policies]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return lines


# --------------------------------------------------------------------------- targets


def _value(results: Results, policy: str, metric: str) -> float | None:
    estimate = results.get(policy, {}).get(metric)
    return None if estimate is None else estimate.value


def _values(results: Results, needs: Sequence[tuple[str, str]]) -> list[float] | str:
    """The point values, or why the target cannot be measured."""
    values = [_value(results, p, m) for p, m in needs]
    gaps = [f"{p} {m}" for (p, m), v in zip(needs, values, strict=True) if v is None]
    if gaps:
        return f"Not measured: undefined in this run: {', '.join(gaps)}."
    return [v for v in values if v is not None]


def _ci_note(results: Results, metric: str, ok: Callable[[float], bool], kind: Kind) -> str:
    estimate = results.get(DELPHI, {}).get(metric)
    if estimate is None or estimate.low is None or estimate.high is None:
        return ""
    if ok(estimate.low) and ok(estimate.high):
        return ""
    return (
        f" The 95% CI [{fmt(estimate.low, kind)}, {fmt(estimate.high, kind)}] crosses the "
        "threshold, so the result is not conclusive."
    )


def _result(target: str, threshold: str, met: bool, text: str, missed: str = "") -> TargetResult:
    if met:
        return TargetResult(target, threshold, Verdict.MET, f"Met. {text}.")
    return TargetResult(target, threshold, Verdict.MISSED, f"Missed: {text}{missed}.")


def _genuine_hold(results: Results, targets: TargetsConfig) -> TargetResult:
    reduction = targets.genuine_hold_reduction_vs_b1
    target, threshold = (
        "Genuine-hold rate",
        f"≥ {reduction:.0%} lower than B1, at fraud ₹ prevented ≥ B1",
    )
    values = _values(
        results,
        [(p, m) for p in (DELPHI, B1) for m in ("genuine_hold_rate", "fraud_prevented_paise")],
    )
    if isinstance(values, str):
        return TargetResult(target, threshold, Verdict.NOT_MEASURED, values)
    d, dp, b, bp = values
    lower, prevents = d <= (1 - reduction) * b, dp >= bp
    holds = f"DELPHI holds {fmt(d, Kind.SHARE)} of genuine payments, B1 {fmt(b, Kind.SHARE)}"
    money = f"DELPHI prevents {fmt(dp, Kind.RUPEES)} of fraud, B1 {fmt(bp, Kind.RUPEES)}"
    if lower and prevents:
        return _result(target, threshold, True, f"{holds}; {money}")
    why = []
    if not lower:
        why.append(f"the genuine-hold rate is not {reduction:.0%} lower ({holds})")
    if not prevents:
        why.append(f"DELPHI prevents less fraud than B1 ({money})")
    return _result(target, threshold, False, "; and ".join(why))


def _coercion(results: Results, targets: TargetsConfig) -> TargetResult:
    multiple = targets.coercion_catch_multiple_vs_b2
    target, threshold = "Coercion catch rate", f"≥ {multiple:g}× B2"
    metric = "coercion_catch_rate"
    values = _values(results, [(DELPHI, metric), (B2, metric)])
    if isinstance(values, str):
        return TargetResult(target, threshold, Verdict.NOT_MEASURED, values)
    d, b = values
    text = (
        f"DELPHI catches {fmt(d, Kind.SHARE)} of coercion payments whose OTP passes, "
        f"B2 {fmt(b, Kind.SHARE)}"
    )
    return _result(target, threshold, d >= multiple * b, text, f", below {multiple:g}× B2")


def _resolution(results: Results, targets: TargetsConfig) -> TargetResult:
    fraction = targets.median_resolution_fraction_of_b1
    target, threshold = "Median hold resolution time", f"≤ 1/{1 / fraction:g} of B1"
    metric = "hold_resolution_minutes_median"
    values = _values(results, [(DELPHI, metric), (B1, metric)])
    if isinstance(values, str):
        return TargetResult(target, threshold, Verdict.NOT_MEASURED, values)
    d, b = values
    met = not math.isinf(d) and (math.isinf(b) or d <= fraction * b)
    text = f"DELPHI's median is {fmt(d, Kind.MINUTES)}, B1's {fmt(b, Kind.MINUTES)}"
    return _result(target, threshold, met, text, f", above 1/{1 / fraction:g} of B1")


def _label_coverage(results: Results, targets: TargetsConfig) -> TargetResult:
    floor = targets.min_label_coverage_held
    target, threshold = "Label coverage (held)", f"≥ {fmt(floor, Kind.SHARE)}"
    metric = "label_coverage_held"
    values = _values(results, [(DELPHI, metric)])
    if isinstance(values, str):
        return TargetResult(target, threshold, Verdict.NOT_MEASURED, values)
    (d,) = values
    text = (
        f"DELPHI's label coverage is {fmt(d, Kind.SHARE)} "
        f"(B1 {fmt(_value(results, B1, metric), Kind.SHARE)})"
    )
    result = _result(target, threshold, d >= floor, text, f", below {fmt(floor, Kind.SHARE)}")
    note = _ci_note(results, metric, lambda v: v >= floor, Kind.SHARE)
    return TargetResult(target, threshold, result.verdict, result.statement + note)


def _golden_hour(_: Results, targets: TargetsConfig) -> TargetResult:
    return TargetResult(
        "Golden-hour recovery ratio",
        f"≥ {targets.golden_hour_multiple_vs_manual:g}× the manual baseline",
        Verdict.NOT_MEASURED,
        "Not measured: the Chase loop (#112, #113) is not in this run.",
    )


def _due_diligence(results: Results, targets: TargetsConfig) -> TargetResult:
    complete = targets.dd_completeness
    target, threshold = "Due-diligence completeness", fmt(complete, Kind.SHARE)
    values = _values(results, [(DELPHI, "dd_pack_completeness")])
    if isinstance(values, str):
        return TargetResult(target, threshold, Verdict.NOT_MEASURED, values)
    (d,) = values
    text = f"{fmt(d, Kind.SHARE)} of DELPHI's decisions have a complete DD pack"
    return _result(target, threshold, d >= complete, text)


TARGETS: tuple[Callable[[Results, TargetsConfig], TargetResult], ...] = (
    _genuine_hold,
    _coercion,
    _resolution,
    _label_coverage,
    _golden_hour,
    _due_diligence,
)


def assess_targets(results: Results, targets: TargetsConfig) -> list[TargetResult]:
    """One statement per docs/22 §5 target, in its order."""
    return [assess(results, targets) for assess in TARGETS]


# --------------------------------------------------------------------------- the report


def render_report(
    results: Results,
    *,
    targets: TargetsConfig,
    reproducibility: Reproducibility,
    on: date,
) -> str:
    """``docs/reports/evaluation-<on>.md`` (docs/22 §6)."""
    lines = [f"# Evaluation report, {on.isoformat()}", ""]
    lines += ["## Results", "", "Value [95% cluster-bootstrap CI over customers].", ""]
    lines += _table(results, [p for p in HEADLINE if p in results])
    ablations = [p for p in ABLATIONS if p in results]
    if ablations:
        lines += ["", "### Ablations", ""]
        lines += _table(results, ablations)
    yields = sorted(k for k in results.get(DELPHI, {}) if k.startswith(f"{INFO_YIELD}["))
    if yields:
        lines += [
            "",
            "### Information yield per probe (DELPHI)",
            "",
            "| Probe | Bits |",
            "|---|---|",
        ]
        for key in yields:
            probe = key[len(INFO_YIELD) + 1 : -1]
            lines.append(f"| {probe} | {cell(results[DELPHI][key], Kind.NUMBER)} |")
    lines += ["", "Not measured in this run:", ""]
    lines += [f"- {metric}: {why}." for metric, why in NOT_MEASURED]
    lines += ["", "## Pre-registered targets (docs/22 §5)", ""]
    lines += ["| Target | Threshold | Verdict |", "|---|---|---|"]
    assessed = assess_targets(results, targets)
    lines += [f"| {t.target} | {t.threshold} | {t.verdict.value} |" for t in assessed]
    lines.append("")
    lines += [f"- **{t.target}.** {t.statement}" for t in assessed]
    lines += ["", "## Caveats", ""]
    lines += [f"- {c}" for c in CAVEATS]
    r = reproducibility
    seeds = ", ".join(f"{k} {v}" for k, v in sorted(r.seeds.items()))
    lines += [
        "",
        "## Reproducibility",
        "",
        f"- Dataset: `{r.dataset_id}` (generator {r.generator_version})",
        f"- Seeds: {seeds}",
        f"- KB: {r.kb_version}, sha256 `{r.kb_sha256}`",
        f"- Code: `{r.git_sha}`",
        "",
    ]
    return "\n".join(lines)
