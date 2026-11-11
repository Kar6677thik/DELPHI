"""``TemplateLlm`` (docs/15 §2, §7): the deterministic provider. CI and tests use it, so does
``DELPHI_LLM_PROVIDER=template``, and the service falls back to it whenever the model's output
is refused or fails validation.

Each evidence source has a Jinja2 template, ``templates/EVIDENCE_NARRATIVE/<SOURCE_KEY>.j2``,
that renders the facts its adapter returns (the payload contract is in docs/15 §7). Templates
are the reference for the numeric-fidelity check: what they print always passes it.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from delphi.llm.port import LlmRequest, LlmResult, LlmStatus, Purpose
from delphi.llm.schemas import (
    SUMMARY_MAX_CHARS,
    CaseSummary,
    EvidenceNarrative,
    TypologyDescription,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
PROVIDER = "template"
_EVENTS = {"SIM_SWAP_REPORTED": "SIM swap", "DEVICE_LOST_REPORTED": "lost device"}


def indian_grouping(rupees: float) -> str:
    """``480000`` → ``4,80,000``; ``1234.5`` → ``1,234.50`` (lakh and crore grouping)."""
    whole = int(rupees)
    fraction = round(rupees - whole, 2)
    digits = str(abs(whole))
    if len(digits) > 3:  # noqa: PLR2004 (the last three digits group together)
        head, tail = digits[:-3], digits[-3:]
        pairs = [head[max(i - 2, 0) : i] for i in range(len(head), 0, -2)][::-1]
        digits = ",".join([*pairs, tail])
    text = ("-" if whole < 0 else "") + digits
    return f"{text}.{round(fraction * 100):02d}" if fraction else text


def _inr(paise: int) -> str:
    return indian_grouping(paise / 100)


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _when(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")


def _words(code: str) -> str:
    return code.replace("_", " ").lower()


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(default=True, default_for_string=True),
        undefined=StrictUndefined,  # a missing fact is a bug, not an empty string
        keep_trailing_newline=False,
    )
    env.filters.update(inr=_inr, num=_num, when=_when, words=_words, event=_EVENTS.get)
    return env


_ENV = _environment()


def render_narrative(payload: Mapping[str, Any]) -> str:
    """The evidence card's sentence for one evidence record (docs/15 §7)."""
    template = _ENV.get_template(f"EVIDENCE_NARRATIVE/{payload['source_key']}.j2")
    return template.render(
        f=payload["facts"],
        subject=payload["subject"],
        as_of=payload["as_of"],
        record_id=payload["record_id"],
    ).strip()


class TemplateLlm:
    """Renders the deterministic text for every purpose but QUESTION_VARIANTS, which has no
    template: without a model, no variants are drafted (docs/15 §1)."""

    @property
    def name(self) -> str:
        return PROVIDER

    def generate(self, request: LlmRequest) -> LlmResult:
        started = time.perf_counter()
        payload = request.user_payload
        parsed: EvidenceNarrative | CaseSummary | TypologyDescription
        if request.purpose is Purpose.EVIDENCE_NARRATIVE:
            parsed = EvidenceNarrative(
                finding=render_narrative(payload), citations=[payload["record_id"]]
            )
        elif request.purpose is Purpose.CASE_SUMMARY:
            records = list(payload["records"])
            sentences = [render_narrative(r) for r in records]
            summary = " ".join(sentences)
            if len(summary) > SUMMARY_MAX_CHARS:  # keep whole sentences that fit
                kept: list[str] = []
                for sentence in sentences:
                    if len(" ".join([*kept, sentence])) > SUMMARY_MAX_CHARS:
                        break
                    kept.append(sentence)
                records, summary = records[: len(kept)], " ".join(kept)
            parsed = CaseSummary(summary=summary, citations=[r["record_id"] for r in records])
        elif request.purpose is Purpose.TYPOLOGY_DESCRIPTION:
            description = (
                _ENV.get_template("TYPOLOGY_DESCRIPTION/default.j2")
                .render(stats=payload["stats"], record_id=payload["record_id"])
                .strip()
            )
            parsed = TypologyDescription(
                name=str(payload["suggested_name"]), description=description
            )
        else:
            return LlmResult(LlmStatus.DISABLED, None, PROVIDER, None, error="no template")
        latency = int((time.perf_counter() - started) * 1000)
        return LlmResult(LlmStatus.OK, parsed, PROVIDER, None, latency_ms=latency)
