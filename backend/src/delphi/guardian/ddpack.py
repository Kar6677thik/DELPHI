"""The due-diligence pack of a Guardian session (docs/09 §7, FR-GDN-15).

Built from what is stored: the session, its probe runs, the payment, the pinned KB version and
the audit entries about the session. The last planning step (the one that stopped) has no probe
run; its candidates table is in the `guardian.session.decided` audit entry
(``final_candidates``), and sections 3 and 4 read it from there. Sections 6 (narratives) and 7
(liens and tickets) are "not applicable" to a session; sections 3 and 4 are too when the session
took the fast path. The pack is complete when every required section is non-empty (docs/09 §7
table), and an undecided session never is: section 5 needs the engine's decision.

The HTML rendering is a plain, printable page with no scripts; every value is escaped.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from delphi.guardian.views import SessionRow, get_session_row, probe_run_rows
from delphi.kb.store import get_version
from delphi.llm.minimise import pseudonym
from delphi.platform.audit import verify_entity
from delphi.platform.errors import NotFoundError
from delphi.platform.tables import audit_log
from delphi.sim import api as bank

FAST_PATH = "FAST_PATH"
CUSTOMER_PREFIX = "CUST"
DECIDED_ACTION = "guardian.session.decided"
STOPPED = "STOPPED"
NO_AUDIT_ENTRIES = "No audit entries for this session."
NOT_APPLICABLE = "Not applicable"
TOP_HYPOTHESES = 3  # docs/09 §7 section 2
TOP_FEATURES = 5
SECTION_TITLES = (
    "Header",
    "Prior",
    "Observation timeline",
    "Guardrails applied",
    "Decisions",
    "Narratives",
    "Liens and tickets",
    "Audit verification",
    "Method note",
)


@dataclass(frozen=True, slots=True)
class Section:
    number: int
    title: str
    required: bool
    content: Any  # None or empty when there is nothing to show
    note: str | None = None  # why a section is not applicable

    @property
    def filled(self) -> bool:
        return self.content not in (None, [], {}, "")


@dataclass(frozen=True, slots=True)
class DdPack:
    session_id: UUID
    sections: tuple[Section, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return all(s.filled for s in self.sections if s.required)

    def as_json(self) -> dict[str, Any]:
        return {
            "session_id": str(self.session_id),
            "complete": self.complete,
            "sections": [
                {
                    "number": s.number,
                    "title": s.title,
                    "required": s.required,
                    "content": s.content if s.required or s.filled else NOT_APPLICABLE,
                    **({"note": s.note} if s.note else {}),
                }
                for s in self.sections
            ],
        }


def _pseudonym(customer_id: UUID | None) -> str | None:
    """A stable pseudonym, so the pack names no customer (docs/19 privacy): the same
    ``CUST-…`` label the LLM payloads use (docs/15 §6)."""
    return None if customer_id is None else pseudonym(CUSTOMER_PREFIX, customer_id)


def _vulnerable(session: Session, customer_id: UUID | None) -> bool:
    if customer_id is None:
        return False
    return bool(
        session.execute(
            select(bank.customers.c.is_vulnerable).where(bank.customers.c.id == customer_id)
        ).scalar_one_or_none()
    )


def _top(probs: Mapping[str, float] | None, n: int) -> list[dict[str, Any]]:
    ranked = sorted((probs or {}).items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [{"hypothesis": h, "probability": round(p, 6)} for h, p in ranked]


def _alternative(c: Mapping[str, Any]) -> dict[str, Any]:
    return {k: c.get(k) for k in ("source_key", "net_value_paise", "eligible", "ineligible_reason")}


def _decided_payload(session: Session, session_id: UUID) -> Mapping[str, Any]:
    """The payload of the session's decided audit entry, empty before the decision."""
    log = audit_log.c
    payload = session.execute(
        select(log.payload)
        .where(
            log.entity_type == "guardian_session",
            log.entity_id == str(session_id),
            log.action == DECIDED_ACTION,
        )
        .order_by(log.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return payload if isinstance(payload, Mapping) else {}


def _final_candidates(decided: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    final = decided.get("final_candidates")
    return [c for c in final if isinstance(c, Mapping)] if isinstance(final, list) else []


def _stop_step(row: SessionRow, final: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The planning step that stopped: no probe ran, every candidate was an alternative."""
    return {
        "seq": None,
        "source": None,
        "mode": None,
        "status": STOPPED,
        "selected_by": None,
        "propensity": None,
        "net_value_paise": None,
        "alternatives": [_alternative(c) for c in final],
        "raw_facts": None,
        "outcome": row.terminal_reason,
        "posterior_before": row.posterior,
        "posterior_after": row.posterior,
        "started_at": row.decided_at.isoformat() if row.decided_at else None,
        "completed_at": row.decided_at.isoformat() if row.decided_at else None,
    }


def _timeline(
    runs: Sequence[Any], row: SessionRow, final: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    steps = [
        {
            "seq": r.seq,
            "source": r.probe_key,
            "mode": r.mode,
            "status": r.status,
            "selected_by": r.selected_by,
            "propensity": r.propensity,
            "net_value_paise": r.net_value_paise,
            "alternatives": [
                _alternative(c) for c in (r.candidates or []) if c.get("source_key") != r.probe_key
            ],
            "raw_facts": r.raw_response,
            "outcome": r.outcome,
            "posterior_before": r.posterior_before,
            "posterior_after": r.posterior_after,
            "started_at": r.started_at.isoformat(),
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in runs
        if r.status not in ("PENDING",)
    ]
    if final:
        steps.append(_stop_step(row, final))
    return steps


def _guardrails(
    runs: Sequence[Any], final: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]] | str:
    """Every source a planning step found ineligible, with its reason (docs/09 §7 section 4),
    including the step that stopped."""
    seen: dict[tuple[str, str], None] = {}
    tables = [list(r.candidates or []) for r in runs] + [list(final)]
    for candidates in tables:
        for c in candidates:
            if not c.get("eligible", True) and c.get("ineligible_reason"):
                seen[str(c["source_key"]), str(c["ineligible_reason"])] = None
    if not seen:
        return "None applied."
    return [{"source": s, "reason": reason} for s, reason in seen]


def build(session: Session, session_id: UUID) -> DdPack:
    row = get_session_row(session, session_id)
    if row is None:
        raise NotFoundError(f"No Guardian session {session_id}.")
    return _build(session, row)


def _build(session: Session, row: SessionRow) -> DdPack:
    runs = probe_run_rows(session, row.id)
    final = _final_candidates(_decided_payload(session, row.id))
    version = get_version(session, row.kb_version_id)
    fast = row.terminal_reason == FAST_PATH
    payment = row.payment
    header = {
        "session_id": str(row.id),
        "payment_id": str(payment.id),
        "subject": "PAYMENT",
        "amount_paise": payment.amount_paise,
        "channel": payment.channel,
        "customer": _pseudonym(payment.payer_customer_id),
        "vulnerable": _vulnerable(session, payment.payer_customer_id),
        "kb_version_id": row.kb_version_id,
        "kb_content_sha256": version.content_sha256 if version else None,
        "started_at": row.started_at.isoformat(),
    }
    prior = (
        {
            "p_model_top": _top(row.p_model, TOP_HYPOTHESES),
            "top_features": list(row.top_features)[:TOP_FEATURES],
            "anomaly_percentile": row.anomaly_percentile,
            "open_mass": row.open_mass,
        }
        if row.prior is not None
        else None
    )
    decisions = (
        {
            "engine": {
                "decision": row.decision,
                "terminal_reason": row.terminal_reason,
                "risk_by_decision_paise": {
                    d: round(r) for d, r in (row.risk_by_decision or {}).items()
                },
                "posterior": row.posterior,
                "baselines": row.baselines,
            },
            "human": None,  # a session has no disposition; its case's DD pack carries one
        }
        if row.decision is not None
        else None
    )
    audit = verify_entity(session, entity_type="guardian_session", entity_id=str(row.id))
    method = (
        "Bayesian decision engine; naive-Bayes conditional-independence assumption; "
        f"likelihoods from KB version {row.kb_version_id}."
    )
    sections = (
        Section(1, SECTION_TITLES[0], True, header),
        Section(2, SECTION_TITLES[1], True, prior),
        Section(
            3,
            SECTION_TITLES[2],
            not fast,
            None if fast else _timeline(runs, row, final),
            "Fast path: no observations." if fast else None,
        ),
        Section(4, SECTION_TITLES[3], not fast, None if fast else _guardrails(runs, final)),
        Section(5, SECTION_TITLES[4], True, decisions),
        Section(6, SECTION_TITLES[5], False, None, "No narratives for a Guardian session."),
        Section(7, SECTION_TITLES[6], False, None, "No chase for a Guardian session."),
        Section(
            8,
            SECTION_TITLES[7],
            True,
            {"ok": audit.ok, "entries_checked": audit.checked, "first_bad_id": audit.first_bad_id}
            if audit.ok and audit.checked
            else None,
            _audit_note(audit.ok, audit.checked, audit.first_bad_id),
        ),
        Section(9, SECTION_TITLES[8], True, method),
    )
    return DdPack(session_id=row.id, sections=sections)


def _audit_note(ok: bool, checked: int, first_bad_id: int | None) -> str | None:
    if not ok:
        return f"The audit chain is broken at entry {first_bad_id}."
    return None if checked else NO_AUDIT_ENTRIES


def _html_value(value: Any) -> str:
    if isinstance(value, Mapping):
        rows = "".join(
            f"<tr><th>{html.escape(str(k))}</th><td>{_html_value(v)}</td></tr>"
            for k, v in value.items()
        )
        return f"<table>{rows}</table>"
    if isinstance(value, list):
        return "<ol>" + "".join(f"<li>{_html_value(v)}</li>" for v in value) + "</ol>"
    return html.escape("—" if value is None else str(value))


def render_html(pack: DdPack) -> str:
    """A printable page (docs/09 §7): every section in order, values escaped."""
    data = pack.as_json()
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>DD pack {html.escape(data['session_id'])}</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;color:#111;background:#fff}"
        "table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:.25rem .5rem;"
        "text-align:left;vertical-align:top}h2{margin-top:1.5rem}</style></head><body>",
        f"<h1>Due-diligence pack: Guardian session {html.escape(data['session_id'])}</h1>",
        f"<p>Complete: <strong>{'yes' if data['complete'] else 'no'}</strong></p>",
    ]
    for s in data["sections"]:
        parts.append(f"<h2>{s['number']}. {html.escape(s['title'])}</h2>")
        if s.get("note"):
            parts.append(f"<p><em>{html.escape(s['note'])}</em></p>")
        parts.append(_html_value(s["content"]))
    parts.append("</body></html>")
    return "".join(parts)
