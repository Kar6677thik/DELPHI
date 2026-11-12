"""Output validation (docs/15 §5). Every check must pass, or the service falls back to the
template (docs/15 §2). The checks never trust the provider: structured outputs already follow
the schema, and the schema is still re-validated here.

1. **Schema**: the result re-validates against the purpose's Pydantic schema.
2. **Citations**: every cited ``record_id`` is in the payload, and there is at least one.
3. **Numeric fidelity**: every number in the text equals a number in the *cited* records,
   after normalisation: rupees ↔ paise / 100, percent ↔ fraction × 100, compared at the
   precision the text shows. Indian digit grouping (``4,80,000``), ``₹`` and ``%`` are read.
   Citation brackets, dates and clock times are not numbers for this check.
4. **No decision language**: a case-insensitive blocklist (``DECISION_PATTERNS``).
5. **Length**: the schema's limits (400 chars for a narrative, 1,200 for a summary).
6. **Question variants**: option keys equal the source's exactly; no URLs, no phone numbers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ValidationError

from delphi.llm.port import Purpose
from delphi.llm.schemas import SCHEMAS, QuestionVariants

DECISION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(confirm(ed|s)?|is|are|was|were)\s+(an?\s+)?(fraud(ster)?|mule|scam(mer)?)s?\b",
        r"\bshould\s+(not\s+)?(be\s+)?(block|hold|held|freez|froze|release|declin|approv)",
        r"\b(must|need\s+to|recommend(ed|s)?|advise[ds]?)\s+(to\s+)?(be\s+)?"
        r"(block|hold|freez|release|declin)",
        r"\bguilty\b",
        r"\binnocent\b",
        r"\b(definitely|certainly|clearly)\s+(a\s+)?(fraud|mule|scam)",
        r"\bcriminal\b",
    )
)
_CITATION = re.compile(r"\[[^\]]*\]")
_DATE_TIME = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"
)
# A number as written: optional ₹, Indian or Western grouping, optional decimals, optional %.
_NUMBER = re.compile(r"(₹\s?)?(?<![\w.])(\d{1,3}(?:,\d{2,3})+|\d+)(\.\d+)?(\s?%)?")
_URL = re.compile(r"https?://|www\.|\b[\w-]+\.(com|in|org|net|io)\b", re.IGNORECASE)
_PHONE = re.compile(r"(?:\+?\d[\s-]?){10,}")


@dataclass(frozen=True, slots=True)
class Validation:
    passed: bool
    failures: tuple[str, ...] = field(default=())

    def as_json(self) -> dict[str, Any]:
        return {"passed": self.passed, "failures": list(self.failures)}


# ------------------------------------------------------------------------------ the payload


def _records(payload: Any) -> Iterator[Mapping[str, Any]]:
    """Every object in the payload that has a ``record_id``."""
    if isinstance(payload, Mapping):
        if "record_id" in payload:
            yield payload
        for value in payload.values():
            yield from _records(value)
    elif isinstance(payload, list | tuple):
        for item in payload:
            yield from _records(item)


def _numbers_in(value: Any) -> Iterator[Decimal]:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        yield Decimal(str(value))
    elif isinstance(value, str):
        cleaned = _DATE_TIME.sub(" ", _CITATION.sub(" ", value))
        for m in _NUMBER.finditer(cleaned):
            yield Decimal(m.group(2).replace(",", "") + (m.group(3) or ""))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if key == "record_id":
                continue
            for number in _numbers_in(item):
                yield number
                if isinstance(key, str) and key.endswith("_paise"):
                    yield number / 100  # paise → rupees
                if isinstance(item, float) and 0 <= item <= 1:
                    yield number * 100  # fraction → percent
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _numbers_in(item)


def _allowed_numbers(payload: Mapping[str, Any], cited: set[str]) -> set[Decimal]:
    allowed: set[Decimal] = set()
    for record in _records(payload):
        if str(record["record_id"]) in cited:
            allowed.update(_numbers_in(record))
    return allowed


def _written_numbers(text: str) -> Iterator[tuple[str, Decimal]]:
    cleaned = _DATE_TIME.sub(" ", _CITATION.sub(" ", text))
    for m in _NUMBER.finditer(cleaned):
        raw = m.group(0).strip()
        try:
            yield raw, Decimal(m.group(2).replace(",", "") + (m.group(3) or ""))
        except InvalidOperation:  # pragma: no cover (the pattern only matches digits)
            continue


def _matches(written: Decimal, allowed: set[Decimal]) -> bool:
    """Equal at the precision the text shows: "4.2" matches 4.21, "4" matches 4.0."""
    exponent = written.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    quantum = Decimal(1).scaleb(-places)
    return any(a.quantize(quantum) == written for a in allowed)


# ------------------------------------------------------------------------------- the checks


def _text_of(parsed: BaseModel) -> list[str]:
    fields = ("finding", "summary", "description", "name")
    return [str(getattr(parsed, f)) for f in fields if hasattr(parsed, f)]


def _check_text(
    texts: Sequence[str], payload: Mapping[str, Any], cited: set[str], failures: list[str]
) -> None:
    allowed = _allowed_numbers(payload, cited)
    for text in texts:
        for raw, number in _written_numbers(text):
            if not _matches(number, allowed):
                failures.append(f"number {raw!r} is not in the cited records")
        for pattern in DECISION_PATTERNS:
            if (m := pattern.search(text)) is not None:
                failures.append(f"decision language: {m.group(0)!r}")


def _check_variants(
    variants: QuestionVariants, payload: Mapping[str, Any], failures: list[str]
) -> None:
    source_keys = sorted(str(o["key"]) for o in payload.get("options", []))
    for v in variants.variants:
        if sorted(o.key for o in v.options) != source_keys:
            failures.append(f"{v.language}: option keys differ from the source's")
        for text in [v.text, *(o.text for o in v.options)]:
            if _URL.search(text):
                failures.append(f"{v.language}: contains a link")
            if _PHONE.search(text):
                failures.append(f"{v.language}: contains a phone number")


def validate(purpose: Purpose, parsed: BaseModel | None, payload: Mapping[str, Any]) -> Validation:
    """Run every docs/15 §5 check for ``purpose`` against the request's payload."""
    if parsed is None:
        return Validation(False, ("no output",))
    schema = SCHEMAS[purpose]
    try:
        parsed = schema.model_validate(parsed.model_dump())
    except ValidationError as exc:
        return Validation(False, tuple(f"schema: {e['msg']}" for e in exc.errors()))
    failures: list[str] = []
    if isinstance(parsed, QuestionVariants):
        _check_variants(parsed, payload, failures)
        return Validation(not failures, tuple(failures))
    known = {str(r["record_id"]) for r in _records(payload)}
    cited = {str(c) for c in getattr(parsed, "citations", [])} or (
        {str(payload["record_id"])} if "record_id" in payload else set()
    )
    if purpose in (Purpose.EVIDENCE_NARRATIVE, Purpose.CASE_SUMMARY):
        if not getattr(parsed, "citations", None):
            failures.append("no citation")
        unknown = sorted(cited - known)
        if unknown:
            failures.append(f"cites records not in the payload: {unknown}")
    _check_text(_text_of(parsed), payload, cited & known, failures)
    return Validation(not failures, tuple(failures))
