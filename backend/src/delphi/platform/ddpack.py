"""The due-diligence pack's shape (docs/09 §7), shared by the Guardian session's pack and the
case's: nine numbered sections, the completeness rule, and the printable HTML page.

What goes in each section is the feature module's business (``delphi.guardian.ddpack``,
``delphi.navigator.ddpack``); this module only holds the container and renders it, so the two
packs read the same and neither module imports the other (docs/04 §4.1).
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

NOT_APPLICABLE = "Not applicable"
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


def method_note(kb_version_id: int) -> str:
    """docs/09 §7 section 9."""
    return (
        "Bayesian decision engine; naive-Bayes conditional-independence assumption; "
        f"likelihoods from KB version {kb_version_id}."
    )


def audit_note(ok: bool, checked: int, first_bad_id: int | None, *, empty: str) -> str | None:
    """Why section 8 has no content: the chain is broken, or there is nothing to check."""
    if not ok:
        return f"The audit chain is broken at entry {first_bad_id}."
    return None if checked else empty


@dataclass(frozen=True, slots=True)
class Section:
    number: int
    title: str
    required: bool
    content: Any  # None or empty when there is nothing to show
    note: str | None = None  # why a section is not applicable, or empty

    @property
    def filled(self) -> bool:
        return self.content not in (None, [], {}, "")


@dataclass(frozen=True, slots=True)
class DdPack:
    """A pack about one subject: ``id_key`` names its id in the JSON (``session_id`` or
    ``case_id``), ``subject`` how the page's heading names it."""

    id_key: str
    subject_id: UUID
    subject: str
    sections: tuple[Section, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """docs/09 §7: every required section is non-empty."""
        return all(s.filled for s in self.sections if s.required)

    def as_json(self) -> dict[str, Any]:
        return {
            self.id_key: str(self.subject_id),
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
    """A printable page (docs/09 §7): every section in order, every value escaped, no scripts."""
    data = pack.as_json()
    subject_id = html.escape(data[pack.id_key])
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>DD pack {subject_id}</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;color:#111;background:#fff}"
        "table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:.25rem .5rem;"
        "text-align:left;vertical-align:top}h2{margin-top:1.5rem}</style></head><body>",
        f"<h1>Due-diligence pack: {html.escape(pack.subject)} {subject_id}</h1>",
        f"<p>Complete: <strong>{'yes' if data['complete'] else 'no'}</strong></p>",
    ]
    for s in data["sections"]:
        parts.append(f"<h2>{s['number']}. {html.escape(s['title'])}</h2>")
        if s.get("note"):
            parts.append(f"<p><em>{html.escape(s['note'])}</em></p>")
        parts.append(_html_value(s["content"]))
    parts.append("</body></html>")
    return "".join(parts)
