"""`just llm-eval` (docs/15 §8, #104) with no model: the fixed cases, the scoring and the report."""

from __future__ import annotations

from collections import Counter
from datetime import date

import pytest

from delphi.cli import main
from delphi.llm.evaluation import (
    EvalCase,
    load_cases,
    render_report,
    request_for,
    run_eval,
)
from delphi.llm.port import LlmRequest, LlmResult, LlmStatus, Purpose
from delphi.llm.schemas import EvidenceNarrative
from delphi.llm.template import TemplateLlm

CASES = load_cases()


def test_the_fixed_set_has_thirty_cases_across_every_evidence_source() -> None:
    assert len(CASES) == 30 and len({c.id for c in CASES}) == 30
    purposes = Counter(c.purpose for c in CASES)
    assert purposes == {Purpose.EVIDENCE_NARRATIVE: 27, Purpose.CASE_SUMMARY: 3}
    sources = Counter(
        c.payload["source_key"] for c in CASES if c.purpose is Purpose.EVIDENCE_NARRATIVE
    )
    assert len(sources) == 9 and set(sources.values()) == {3}


def test_the_templates_pass_every_case() -> None:
    """The templates are the reference output (docs/15 §7), so the cases themselves are sound:
    a model that writes what the templates write passes."""
    report = run_eval(TemplateLlm(), CASES)
    assert report.passed, [r for r in report.results if r.failures]
    assert report.citation_validity == report.numeric_fidelity == 1.0
    assert report.fallback_rate == 0.0 and report.decision_violations == 0


class Scripted:
    """A fake provider: answers each case id with a scripted narrative, status or error."""

    name = "fake"

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.by_record = {
            str(c.payload.get("record_id")): c.id for c in CASES if "record_id" in c.payload
        }

    def generate(self, request: LlmRequest) -> LlmResult:
        answer = self.answers[self.by_record[str(request.user_payload["record_id"])]]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, LlmStatus):
            return LlmResult(answer, None, self.name, "fake-model")
        assert isinstance(answer, EvidenceNarrative)
        return LlmResult(LlmStatus.OK, answer, self.name, "fake-model", 100, 20, 900)


def _case(case_id: str) -> EvalCase:
    return next(c for c in CASES if c.id == case_id)


def test_each_failure_kind_is_scored() -> None:
    fan_in = _case("inbound_fan_in-1")
    record = fan_in.payload["record_id"]
    cases = [_case(i) for i in ("inbound_fan_in-1", "inbound_fan_in-2", "linked_complaints-1")]
    cases += [
        _case("linked_complaints-2"),
        _case("linked_complaints-3"),
        _case("account_profile-1"),
    ]
    other = _case("inbound_fan_in-2").payload["record_id"]
    answers: dict[str, object] = {
        # good: the facts' number, cited
        "inbound_fan_in-1": EvidenceNarrative(
            finding=f"12 unrelated payers credited the account in 48 h [{record}].",
            citations=[record],
        ),
        # wrong number
        "inbound_fan_in-2": EvidenceNarrative(
            finding=f"7 unrelated payers credited it [{other}].", citations=[other]
        ),
        # cites a record that is not in its payload
        "linked_complaints-1": EvidenceNarrative(
            finding="Two complaints name the account.", citations=["not-a-record"]
        ),
        # decision language
        "linked_complaints-2": EvidenceNarrative(
            finding="No complaints; this is a mule account.",
            citations=[_case("linked_complaints-2").payload["record_id"]],
        ),
        "linked_complaints-3": LlmStatus.REFUSED,
        "account_profile-1": TimeoutError("slow"),
    }
    report = run_eval(Scripted(answers), cases)
    assert not report.passed
    assert report.citation_validity == pytest.approx(3 / 4)  # of the 4 model outputs
    assert report.numeric_fidelity == pytest.approx(3 / 4)
    assert report.decision_violations == 1
    assert report.fallback_rate == pytest.approx(5 / 6)
    statuses = {r.case_id: r.status for r in report.results}
    assert statuses["linked_complaints-3"] is LlmStatus.REFUSED
    assert statuses["account_profile-1"] is LlmStatus.ERROR
    text = render_report(report, on=date(2026, 11, 22), git_sha="abc1234")
    assert "**Result: FAIL**" in text and "| Decision-language violations | 1 | **no** |" in text
    assert "| linked_complaints-3 | REFUSED | REFUSED |" in text


def test_the_request_is_the_one_the_service_sends() -> None:
    request = request_for(_case("payee_relationship-1"))
    assert request.purpose is Purpose.EVIDENCE_NARRATIVE and request.prompt_version
    assert request.user_payload["facts"]["max_prior_amount_paise"] == 48_000_000


def test_the_command_refuses_to_run_without_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELPHI_LLM_PROVIDER", "template")
    assert main(["llm-eval"]) == 2
