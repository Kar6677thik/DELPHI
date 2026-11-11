"""The structured diff between two KB versions (docs/16 §2, FR-KB-04).

Both sides are the versions' canonical JSON content. Objects are compared key by key (in sorted
order, so the result is deterministic) and lists element by element when they have the same
length. A list whose length changed is reported as one ``changed`` entry for the whole list,
because KB lists (orders, likelihood rows, variants) are read as a unit.

Each change names its location as a JSON Pointer (RFC 6901), e.g.
``/hypotheses/ATO/base_rate``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DiffOp(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class Change:
    path: str
    op: DiffOp
    before: Any = None
    after: Any = None


def _pointer(parts: tuple[str, ...]) -> str:
    return "".join("/" + p.replace("~", "~0").replace("/", "~1") for p in parts)


def _walk(before: Any, after: Any, at: tuple[str, ...], out: list[Change]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            here = (*at, str(key))
            if key not in after:
                out.append(Change(_pointer(here), DiffOp.REMOVED, before=before[key]))
            elif key not in before:
                out.append(Change(_pointer(here), DiffOp.ADDED, after=after[key]))
            else:
                _walk(before[key], after[key], here, out)
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for i, (b, a) in enumerate(zip(before, after, strict=True)):
            _walk(b, a, (*at, str(i)), out)
    elif type(before) is not type(after) or before != after:
        out.append(Change(_pointer(at), DiffOp.CHANGED, before=before, after=after))


def diff_content(before: Any, after: Any) -> list[Change]:
    """What changed from ``before`` to ``after`` (two KB contents as canonical JSON values)."""
    out: list[Change] = []
    _walk(before, after, (), out)
    return out
