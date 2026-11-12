"""Versioned prompts (docs/15 §4): ``prompts/<PURPOSE>/v<N>.md``. The version is stored with
every call, so an output can always be traced to the exact prompt that produced it."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from delphi.llm.port import Purpose

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CURRENT: dict[Purpose, str] = {p: "v1" for p in Purpose}


@cache
def load_prompt(purpose: Purpose, version: str | None = None) -> tuple[str, str]:
    """``(version, system prompt)`` for ``purpose``; the current version by default."""
    version = version or CURRENT[purpose]
    path = PROMPTS_DIR / purpose.value / f"{version}.md"
    return version, path.read_text(encoding="utf-8").strip()
