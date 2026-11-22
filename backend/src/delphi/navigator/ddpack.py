"""The due-diligence pack of a case (docs/09 §7, FR-NAV-11, NFR-07).

Built from what is stored: the case, its current evidence records with their narratives, the
pinned KB version and the audit entries about the case. A case opened by a Guardian hold also
inherits its session's prior, observations, guardrails and audit check. The Navigator may not
import Guardian (docs/04 §4.1), so the caller passes those in as an ``InheritedTrail``: the api
and the worker build it from ``guardian.api.build_dd_pack``.

Completeness by section (docs/09 §7 table, "Case" column): 1, 2, 3, 5, 6, 8 and 9 are required;
4 is required only for Guardian-originated observations; 7 waits for the chase (M6) and is "not
applicable" until then. Section 5 needs the human disposition, so an open case is never complete.
Section 6 needs a narrative (LLM or template) for every evidence record; a case decided without
any evidence has nothing to narrate, which the section says instead of staying empty.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from delphi.kb.store import get_version
from delphi.llm.minimise import pseudonym
from delphi.navigator.disposition import vulnerable_payer
from delphi.navigator.store import CaseRecord, CaseStatus, get_case
from delphi.navigator.tables import cases, evidence_records
from delphi.platform.audit import verify_entity
from delphi.platform.ddpack import (
    SECTION_TITLES,
    DdPack,
    Section,
    audit_note,
    method_note,
)
from delphi.platform.errors import NotFoundError
from delphi.platform.logging import get_logger
from delphi.sim import api as bank

log = get_logger(__name__)

CUSTOMER_PREFIX = "CUST"
TOP_HYPOTHESES = 3  # docs/09 §7 section 2
NO_AUDIT_ENTRIES = "No audit entries for this case."
NO_EVIDENCE = "No evidence was fetched for this case."
NO_CHASE = "No chase for this case's subject (the chase arrives in M6)."
AI, TEMPLATE = "AI-written summary", "Template summary"


@dataclass(frozen=True, slots=True)
class InheritedTrail:
    """What a case inherits from its Guardian session's DD pack (sections 2, 3, 4 and 8)."""

    prior: Any = None
    timeline: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    guardrails: Any = None
    audit: Mapping[str, Any] | None = None
    audit_note: str | None = None


# How the api and the worker give the Navigator its session's trail without an import cycle.
GuardianTrail = Callable[[Session, UUID], InheritedTrail | None]


def _top(probs: Mapping[str, float] | None, n: int) -> list[dict[str, Any]]:
    ranked = sorted((probs or {}).items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [{"hypothesis": h, "probability": round(p, 6)} for h, p in ranked]


def _header(session: Session, case: CaseRecord) -> dict[str, Any]:
    version = get_version(session, case.kb_version_id)
    payment = (
        bank.get_payment(session, case.subject_payment_id)
        if case.subject_payment_id is not None
        else None
    )
    customer = payment.payer_customer_id if payment is not None else None
    return {
        "case_id": str(case.id),
        "type": case.type.value,
        "protocol": case.protocol.value,
        "subject": "PAYMENT" if case.subject_payment_id is not None else "ACCOUNT",
        "payment_id": str(case.subject_payment_id) if case.subject_payment_id else None,
        "account_id": str(case.subject_account_id) if case.subject_account_id else None,
        "guardian_session_id": str(case.guardian_session_id) if case.guardian_session_id else None,
        "amount_paise": case.amount_at_stake_paise,
        "channel": payment.channel if payment is not None else None,
        "customer": None if customer is None else pseudonym(CUSTOMER_PREFIX, customer),
        "vulnerable": vulnerable_payer(session, case),
        "kb_version_id": case.kb_version_id,
        "kb_content_sha256": version.content_sha256 if version else None,
        "opened_at": case.opened_at.isoformat(),
    }


def _prior(case: CaseRecord, inherited: InheritedTrail | None) -> Any:
    """The session's prior section when the case has one, else what the case stored of it."""
    if inherited is not None and inherited.prior:
        return inherited.prior
    stored = case.prior_components or {}
    if not stored.get("p_model") and not stored.get("prior"):
        return None
    return {
        "p_model_top": _top(stored.get("p_model") or stored.get("prior"), TOP_HYPOTHESES),
        "top_features": [],  # kept only in the session's pack
        "anomaly_percentile": stored.get("anomaly_percentile"),
        "open_mass": stored.get("open_mass"),
    }


def _records(session: Session, case_id: UUID) -> list[Any]:
    """The current (not superseded) records with their stored citations, oldest first."""
    e = evidence_records.c
    return list(
        session.execute(
            select(evidence_records)
            .where(e.case_id == case_id, e.superseded_by.is_(None))
            .order_by(e.requested_at, e.id)
        )
    )


def _step(r: Any) -> dict[str, Any]:
    return {
        "origin": "NAVIGATOR",
        "evidence_record_id": str(r.id),
        "source": r.source_key,
        "selected_by": "INVESTIGATOR",
        "requested_by": str(r.requested_by) if r.requested_by else None,
        "rank_at_request": r.rank_at_request,
        "net_value_paise": r.net_value_at_request_paise,
        "raw_facts": r.raw,
        "outcome": r.outcome,
        "posterior_before": r.posterior_before,
        "posterior_after": r.posterior_after,
        "requested_at": r.requested_at.isoformat(),
    }


def _narratives(records: Sequence[Any]) -> tuple[Any, str | None]:
    """Section 6: every record's narrative, labelled, with its citations resolved to the cited
    records' sources. Empty (incomplete) while any record still waits for its narrative."""
    if not records:
        return NO_EVIDENCE, None
    waiting = [r.source_key for r in records if not r.narrative]
    if waiting:
        return None, f"Waiting for narratives: {', '.join(waiting)}."
    sources = {str(r.id): r.source_key for r in records}
    return [
        {
            "evidence_record_id": str(r.id),
            "source": r.source_key,
            "label": AI if r.narrative_source == "LLM" else TEMPLATE,
            "text": r.narrative,
            "citations": [
                {"record_id": c, "source": sources.get(str(c))}
                for c in (r.narrative_citations or [])
            ],
        }
        for r in records
    ], None


def _decisions(case: CaseRecord) -> dict[str, Any] | None:
    """Section 5: the engine part and the human disposition; nothing until the case is decided."""
    if case.status is not CaseStatus.DISPOSED or case.disposition is None:
        return None
    return {
        "engine": {
            "recommended_decision": case.recommended_decision,
            "risk_by_decision_paise": {
                d: round(r) for d, r in (case.risk_by_decision or {}).items()
            },
            "posterior": case.posterior,
        },
        "human": {
            "decision": case.disposition,
            "typology": case.disposition_typology,
            "confidence": case.disposition_confidence,
            "is_override": case.is_override,
            "override_rationale": case.override_rationale,
            "disposed_by": str(case.disposed_by) if case.disposed_by else None,
            "disposed_at": case.disposed_at.isoformat() if case.disposed_at else None,
        },
    }


def _audit(session: Session, case: CaseRecord, inherited: InheritedTrail | None) -> Section:
    audit = verify_entity(session, entity_type="case", entity_id=str(case.id))
    note = audit_note(audit.ok, audit.checked, audit.first_bad_id, empty=NO_AUDIT_ENTRIES)
    content: dict[str, Any] | None = (
        {"case": {"ok": True, "entries_checked": audit.checked, "first_bad_id": None}}
        if audit.ok and audit.checked
        else None
    )
    if inherited is not None and case.guardian_session_id is not None:
        if inherited.audit is None:  # the session's chain is broken or empty: not verified
            content, note = None, inherited.audit_note or "The session's audit was not verified."
        elif content is not None:
            content["guardian_session"] = dict(inherited.audit)
    return Section(8, SECTION_TITLES[7], True, content, note)


def build(session: Session, case_id: UUID, inherited: InheritedTrail | None = None) -> DdPack:
    case = get_case(session, case_id)
    if case is None:
        raise NotFoundError("No case with this id.")
    records = _records(session, case_id)
    from_guardian = list(inherited.timeline) if inherited is not None else []
    timeline = [{"origin": "GUARDIAN", **dict(s)} for s in from_guardian] + [
        _step(r) for r in records
    ]
    narratives, narratives_note = _narratives(records)
    sections = (
        Section(1, SECTION_TITLES[0], True, _header(session, case)),
        Section(2, SECTION_TITLES[1], True, _prior(case, inherited)),
        Section(3, SECTION_TITLES[2], True, timeline),
        Section(
            4,
            SECTION_TITLES[3],
            bool(from_guardian),  # docs/09 §7: for Guardian-originated observations only
            inherited.guardrails if inherited is not None and from_guardian else None,
            None if from_guardian else "No Guardian-originated observations.",
        ),
        Section(5, SECTION_TITLES[4], True, _decisions(case), _decided_note(case)),
        Section(6, SECTION_TITLES[5], True, narratives, narratives_note),
        Section(7, SECTION_TITLES[6], False, None, NO_CHASE),
        _audit(session, case, inherited),
        Section(9, SECTION_TITLES[8], True, method_note(case.kb_version_id)),
    )
    return DdPack(id_key="case_id", subject_id=case.id, subject="Case", sections=sections)


def _decided_note(case: CaseRecord) -> str | None:
    return None if case.status is CaseStatus.DISPOSED else "The case has no disposition yet."


def refresh_complete(session: Session, case_id: UUID, trail: GuardianTrail | None) -> bool:
    """Recompute and store ``cases.dd_pack_complete`` (docs/09 §7: on every change), in the
    caller's transaction. The flag is derived, so it does not bump the case's version. A failure
    leaves the flag as it was and never blocks the change that asked (docs/09 §7)."""
    try:
        with session.begin_nested():  # a failed read must not abort the caller's transaction
            return _refresh(session, case_id, trail)
    except Exception:
        log.exception("navigator.dd_pack_check_failed", case_id=str(case_id))
        return False


def _refresh(session: Session, case_id: UUID, trail: GuardianTrail | None) -> bool:
    case = get_case(session, case_id)
    if case is None:
        raise NotFoundError("No case with this id.")
    inherited = (
        trail(session, case.guardian_session_id)
        if trail is not None and case.guardian_session_id is not None
        else None
    )
    complete = build(session, case_id, inherited).complete
    if complete != case.dd_pack_complete:
        session.execute(
            update(cases).where(cases.c.id == case_id).values(dd_pack_complete=complete)
        )
    return complete
