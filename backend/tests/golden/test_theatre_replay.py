"""The Theatre shows the engine's real numbers: its committed S-1 replay must be exactly what the
engine computes today for the ENG-S1 fixtures (frontend/src/theatre/replays/s1.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from delphi.kb.model import KnowledgeBase

from .s1_replay import build

REGEN = "regenerate it: uv run python -m tests.golden.s1_replay > <the replay path>"
REPLAY = (
    Path(__file__).resolve().parents[3] / "frontend" / "src" / "theatre" / "replays" / "s1.json"
)


def test_the_committed_replay_is_the_engines_run(kb: KnowledgeBase) -> None:
    committed = json.loads(REPLAY.read_text(encoding="utf-8"))
    fresh = json.loads(json.dumps(build(kb), ensure_ascii=False))
    assert [s["kind"] for s in committed["steps"]] == [s["kind"] for s in fresh["steps"]], REGEN
    for old, new in zip(committed["steps"], fresh["steps"], strict=True):
        assert old.get("probe_key") == new.get("probe_key")
        assert old.get("decision") == new.get("decision")
        assert old["posterior"] == pytest.approx(new["posterior"], abs=1e-4)
        assert old.get("candidates") == new.get("candidates")
    assert committed["hypotheses"] == fresh["hypotheses"]


def test_the_story_is_the_one_eng_s1_pins(kb: KnowledgeBase) -> None:
    """The beats the Theatre narrates: genuine first, the call swings it, the coached answer
    swings it back, the spell-breaker breaks it, and a protective hold."""
    steps = build(kb)["steps"]
    active = [s["probe_key"] for s in steps if s["kind"] == "active"]
    assert active[:2] == ["PURPOSE_QUESTION", "SPELL_BREAKER_DIGITAL_ARREST"]
    by_key = {s.get("probe_key"): s["posterior"] for s in steps}
    assert by_key["SESSION_BEHAVIOUR"]["GENUINE"] > 0.85
    assert (
        by_key["CALL_STATUS"]["APP_DIGITAL_ARREST"]
        > by_key["SESSION_BEHAVIOUR"]["APP_DIGITAL_ARREST"]
    )
    assert by_key["PURPOSE_QUESTION"]["GENUINE"] > by_key["CALL_STATUS"]["GENUINE"]
    assert by_key["SPELL_BREAKER_DIGITAL_ARREST"]["APP_DIGITAL_ARREST"] >= 0.65
    assert steps[-1]["decision"] == "PROTECTIVE_HOLD"
