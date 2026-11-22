"""DD pack completeness (docs/09 §7, FR-GDN-15): only required sections count, and a required
section counts when it has content ("None applied." is content; an empty list is not)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from delphi.guardian.ddpack import NOT_APPLICABLE, DdPack, Section


def _pack(*sections: Section) -> DdPack:
    return DdPack(
        id_key="session_id", subject_id=uuid4(), subject="Guardian session", sections=sections
    )


@pytest.mark.parametrize(
    ("content", "filled"),
    [
        (None, False),
        ([], False),
        ({}, False),
        ("", False),
        ("None applied.", True),  # section 4 stating no guardrail applied is content
        ([{"source": "X", "reason": "FAIRNESS_CAP"}], True),
        ({"ok": True, "entries_checked": 0, "first_bad_id": None}, True),
    ],
)
def test_a_section_is_filled_when_it_has_content(content: Any, filled: bool) -> None:
    assert Section(4, "Guardrails applied", True, content).filled is filled


def test_only_required_sections_decide_completeness() -> None:
    header = Section(1, "Header", True, {"session_id": "s"})
    na = Section(6, "Narratives", False, None, "No narratives for a Guardian session.")
    assert _pack(header, na).complete is True
    assert _pack(header, Section(3, "Observation timeline", True, [])).complete is False
    assert _pack(header, Section(5, "Decisions", True, None)).complete is False


def test_audit_section_with_nothing_checked_is_not_complete() -> None:
    """The builder leaves section 8 empty when no entry was checked, with a note saying so."""
    audit = Section(8, "Audit verification", True, None, "No audit entries for this session.")
    pack = _pack(Section(1, "Header", True, {"a": 1}), audit)
    assert pack.complete is False
    [_, rendered] = pack.as_json()["sections"]
    assert rendered["content"] is None and rendered["note"].startswith("No audit entries")


def test_not_applicable_sections_render_as_such() -> None:
    pack = _pack(Section(7, "Liens and tickets", False, None, "No chase for a Guardian session."))
    [rendered] = pack.as_json()["sections"]
    assert rendered["content"] == NOT_APPLICABLE and rendered["required"] is False
