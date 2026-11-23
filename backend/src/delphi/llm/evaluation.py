"""The LLM path's evaluation (docs/15 §8, #104): ``just llm-eval``.

A fixed set of cases (``tests/llm_eval/cases.jsonl``: 27 evidence narratives, three per source,
and 3 case summaries) goes to the model provider exactly as ``LlmService`` sends it: the payload
minimised, the current prompt, the purpose's schema and token cap. Each output is checked with
the production validators (docs/15 §5). The run passes on the docs/15 §8 criteria:

- **citation validity** 100%: every model output cites only records in its payload, and cites;
- **numeric fidelity** 100%: every number written is one of the cited records' facts;
- **decision-language violations** 0;
- **fallback rate** ≤ 10%: the share of cases where the service would have used the template
  (an error, a refusal, a timeout, or an output that failed validation).

It costs money and is non-deterministic, so it never runs in CI; the report is committed to
``docs/reports/llm-eval-<date>.md``. Nothing here decides anything (ADR-0004).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from delphi.llm.minimise import minimise
from delphi.llm.port import LlmPort, LlmRequest, LlmResult, LlmStatus, Purpose
from delphi.llm.prompts import load_prompt
from delphi.llm.schemas import MAX_TOKENS, SCHEMAS
from delphi.llm.validators import validate

CASES_FILE = Path(__file__).resolve().parents[3] / "tests" / "llm_eval" / "cases.jsonl"
MAX_FALLBACK_RATE = 0.10  # docs/15 §8
_CITATION_FAILURES = ("no citation", "cites records not in the payload")
_NUMBER_FAILURE = "number "
_DECISION_FAILURE = "decision language"
_PERCENT = 100


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    purpose: Purpose
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    purpose: Purpose
    status: LlmStatus  # the provider's status (OK even if validation then failed)
    failures: tuple[str, ...]  # validation failures, or the provider's status
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None

    @property
    def model_output(self) -> bool:
        return self.status is LlmStatus.OK

    @property
    def falls_back(self) -> bool:
        return not self.model_output or bool(self.failures)

    def failed(self, *prefixes: str) -> bool:
        return any(f.startswith(prefixes) for f in self.failures)


@dataclass(frozen=True, slots=True)
class EvalReport:
    provider: str
    model: str | None
    prompt_versions: dict[str, str]
    results: tuple[CaseResult, ...]

    def _share_without(self, *prefixes: str) -> float | None:
        outputs = [r for r in self.results if r.model_output]
        if not outputs:
            return None
        return sum(not r.failed(*prefixes) for r in outputs) / len(outputs)

    @property
    def citation_validity(self) -> float | None:
        return self._share_without(*_CITATION_FAILURES)

    @property
    def numeric_fidelity(self) -> float | None:
        return self._share_without(_NUMBER_FAILURE)

    @property
    def decision_violations(self) -> int:
        return sum(1 for r in self.results for f in r.failures if f.startswith(_DECISION_FAILURE))

    @property
    def fallback_rate(self) -> float:
        return sum(r.falls_back for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def checks(self) -> dict[str, bool]:
        return {
            "citation validity 100%": self.citation_validity == 1.0,
            "numeric fidelity 100%": self.numeric_fidelity == 1.0,
            "decision-language violations 0": self.decision_violations == 0,
            f"fallback rate ≤ {MAX_FALLBACK_RATE:.0%}": self.fallback_rate <= MAX_FALLBACK_RATE,
        }

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(self.checks.values())


def load_cases(path: Path = CASES_FILE) -> list[EvalCase]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw = json.loads(line)
            cases.append(EvalCase(raw["id"], Purpose(raw["purpose"]), raw["payload"]))
    return cases


def request_for(case: EvalCase) -> LlmRequest:
    """The request ``LlmService.generate`` would send for this payload."""
    version, system = load_prompt(case.purpose)
    return LlmRequest(
        purpose=case.purpose,
        prompt_version=version,
        system=system,
        user_payload=minimise(dict(case.payload)),
        output_schema=SCHEMAS[case.purpose],
        max_tokens=MAX_TOKENS[case.purpose],
    )


def _call(provider: LlmPort, request: LlmRequest) -> LlmResult:
    started = time.perf_counter()
    try:
        return provider.generate(request)
    except Exception as exc:  # the service treats a raised error as ERROR, so does the eval
        latency = int((time.perf_counter() - started) * 1000)
        return LlmResult(
            LlmStatus.ERROR, None, provider.name, None, latency_ms=latency, error=type(exc).__name__
        )


def run_eval(provider: LlmPort, cases: Iterable[EvalCase]) -> EvalReport:
    results: list[CaseResult] = []
    versions: dict[str, str] = {}
    model: str | None = None
    for case in cases:
        request = request_for(case)
        versions[case.purpose.value] = request.prompt_version
        result = _call(provider, request)
        model = result.model or model
        if result.status is LlmStatus.OK:
            failures = validate(case.purpose, result.parsed, request.user_payload).failures
        else:
            failures = (result.status.value,)
        results.append(
            CaseResult(
                case.id,
                case.purpose,
                result.status,
                tuple(failures),
                result.input_tokens,
                result.output_tokens,
                result.latency_ms,
            )
        )
    return EvalReport(provider.name, model, versions, tuple(results))


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * _PERCENT:.1f}%"


def render_report(report: EvalReport, *, on: date, git_sha: str) -> str:
    """``docs/reports/llm-eval-<on>.md``."""
    tokens_in = sum(r.input_tokens or 0 for r in report.results)
    tokens_out = sum(r.output_tokens or 0 for r in report.results)
    versions = ", ".join(f"{p} {v}" for p, v in sorted(report.prompt_versions.items()))
    lines = [
        f"# LLM evaluation, {on.isoformat()}",
        "",
        f"**Result: {'PASS' if report.passed else 'FAIL'}** (docs/15 §8).",
        "",
        f"- Provider: {report.provider}; model: {report.model or 'n/a'}",
        f"- Prompts: {versions}",
        f"- Cases: {len(report.results)} (`backend/tests/llm_eval/cases.jsonl`)",
        f"- Tokens: {tokens_in:,} in, {tokens_out:,} out",
        f"- Code: `{git_sha}`",
        "",
        "| Criterion | Value | Met |",
        "|---|---|---|",
        f"| Citation validity | {_pct(report.citation_validity)} | "
        f"{_yes(report.checks['citation validity 100%'])} |",
        f"| Numeric fidelity | {_pct(report.numeric_fidelity)} | "
        f"{_yes(report.checks['numeric fidelity 100%'])} |",
        f"| Decision-language violations | {report.decision_violations} | "
        f"{_yes(report.checks['decision-language violations 0'])} |",
        f"| Fallback rate | {_pct(report.fallback_rate)} | "
        f"{_yes(report.checks[f'fallback rate ≤ {MAX_FALLBACK_RATE:.0%}'])} |",
        "",
        "## Cases",
        "",
        "| Case | Status | Failures | Latency |",
        "|---|---|---|---|",
    ]
    for r in report.results:
        failures = "; ".join(r.failures).replace("|", "\\|") or "—"
        latency = "—" if r.latency_ms is None else f"{r.latency_ms:,} ms"
        lines.append(f"| {r.case_id} | {r.status.value} | {failures} | {latency} |")
    lines.append("")
    return "\n".join(lines)


def _yes(met: bool) -> str:
    return "yes" if met else "**no**"
