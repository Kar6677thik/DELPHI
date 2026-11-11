"""Data minimisation (docs/15 §6): what leaves the database for a provider.

Callers build payloads from pseudonyms (``pseudonym``) and the facts of the records only. As a
second line, ``minimise`` drops fields that name or reach a person, and masks any long digit run
in text (an account or phone number) to its last four digits. Numbers stored as numbers
(amounts, counts) are facts and are kept. The same minimised payload is what ``llm_calls``
stores.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

DROPPED_KEYS = frozenset(
    {"name", "full_name", "display_name", "phone", "phone_number", "email", "address", "dob"}
)
# 10 or more digits, optionally spaced: an account or phone number. Dates and ids (with dashes
# or letters between short digit groups) are left alone.
_LONG_DIGITS = re.compile(r"(?<![\w-])(?:\d ?){9,}\d(?![\w-])")
_KEEP = 4


def pseudonym(prefix: str, entity_id: UUID | str) -> str:
    """``ACC-…5678``: a stable, non-identifying label (the mapping stays in the database)."""
    tail = str(entity_id).replace("-", "")[-_KEEP:].upper()
    return f"{prefix}-…{tail}"


def _mask(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "…" + digits[-_KEEP:]


def minimise(value: Any) -> Any:
    """A copy of ``value`` with personal fields dropped and long digit runs masked."""
    if isinstance(value, Mapping):
        return {k: minimise(v) for k, v in value.items() if str(k).lower() not in DROPPED_KEYS}
    if isinstance(value, list | tuple):
        return [minimise(v) for v in value]
    if isinstance(value, str):
        return _LONG_DIGITS.sub(_mask, value)
    return value
