"""The case DD pack's section rules (docs/09 §7, FR-NAV-11), without a database."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from delphi.navigator.ddpack import (
    AI,
    NO_EVIDENCE,
    TEMPLATE,
    InheritedTrail,
    _decisions,
    _narratives,
    _prior,
)
from delphi.navigator.store import CaseProtocol, CaseRecord, CaseStatus, CaseType

T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


@dataclass
class Rec:
    id: UUID
    source_key: str
    narrative: str | None
    narrative_source: str | None = "TEMPLATE"
    narrative_citations: list[str] | None = None


def _case(**kw: Any) -> CaseRecord:
    base = CaseRecord(
        id=uuid4(),
        type=CaseType.PAYMENT_HOLD,
        protocol=CaseProtocol.COERCION,
        status=CaseStatus.OPEN,
        subject_payment_id=uuid4(),
        subject_account_id=None,
        guardian_session_id=uuid4(),
        kb_version_id=1,
        leading_hypothesis="APP_DIGITAL_ARREST",
        prior_components={"p_model": {"APP_DIGITAL_ARREST": 0.7, "GENUINE": 0.3}},
        posterior={"APP_DIGITAL_ARREST": 0.9, "GENUINE": 0.1},
        recommended_decision="CONFIRM_FRAUD",
        risk_by_decision={"CONFIRM_FRAUD": 1000.4, "RELEASE": 900_000.0},
        amount_at_stake_paise=48_000_000,
        opened_at=T0,
        sla_due_at=T0,
        dd_pack_complete=False,
        unexplained=False,
        disposition=None,
        disposed_at=None,
        is_override=False,
        version=1,
        campaign_id=None,
    )
    return replace(base, **kw)


def test_narratives_need_every_record_narrated() -> None:
    a, b = uuid4(), uuid4()
    content, note = _narratives(
        [Rec(a, "ACCOUNT_PROFILE", "Text."), Rec(b, "LINKED_COMPLAINTS", None)]
    )
    assert content is None and note == "Waiting for narratives: LINKED_COMPLAINTS."


def test_narratives_are_labelled_and_their_citations_resolved() -> None:
    a, b = uuid4(), uuid4()
    content, note = _narratives(
        [
            Rec(a, "ACCOUNT_PROFILE", "New account.", "LLM", [str(a), str(b)]),
            Rec(b, "INBOUND_FAN_IN", "Fourteen payers.", "TEMPLATE", [str(b)]),
        ]
    )
    assert note is None
    assert [n["label"] for n in content] == [AI, TEMPLATE]
    assert content[0]["citations"] == [
        {"record_id": str(a), "source": "ACCOUNT_PROFILE"},
        {"record_id": str(b), "source": "INBOUND_FAN_IN"},
    ]


def test_a_case_without_evidence_has_nothing_to_narrate() -> None:
    assert _narratives([]) == (NO_EVIDENCE, None)


def test_an_open_case_has_no_decisions_section() -> None:
    assert _decisions(_case()) is None


def test_a_decided_case_shows_the_engine_and_the_human() -> None:
    user = uuid4()
    decided = _case(
        status=CaseStatus.DISPOSED,
        disposition="CONFIRM_FRAUD",
        disposition_typology="APP_DIGITAL_ARREST",
        disposition_confidence=5,
        disposed_by=user,
        disposed_at=T0,
    )
    section = _decisions(decided)
    assert section is not None
    assert section["engine"]["risk_by_decision_paise"] == {
        "CONFIRM_FRAUD": 1000,
        "RELEASE": 900_000,
    }
    assert section["human"]["decision"] == "CONFIRM_FRAUD"
    assert section["human"]["disposed_by"] == str(user)


def test_the_prior_comes_from_the_session_else_from_the_case() -> None:
    inherited = InheritedTrail(prior={"p_model_top": [], "top_features": [{"f": 1}]})
    assert _prior(_case(), inherited) == inherited.prior
    fallback = _prior(_case(), None)
    assert fallback["p_model_top"][0] == {"hypothesis": "APP_DIGITAL_ARREST", "probability": 0.7}
    assert _prior(_case(prior_components={}), None) is None
