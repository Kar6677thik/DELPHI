"""The Theatre's S-1 replay (frontend/src/theatre/replays/s1.json), built from the real engine.

It runs the ENG-S1 golden fixtures (``test_story_behaviour.py``: Kamala Devi, coached) through
the engine step by step, as the Guardian would: the passive probes first, then the planner's
choice of active probes until it stops, and records each posterior, the planner's candidates and
the decision. The only scripted inputs are the fixture's prior and Kamala's answers.

Regenerate after a KB or engine change (``test_theatre_replay.py`` fails until you do), from
``backend/``::

    uv run python -m tests.golden.s1_replay > ../frontend/src/theatre/replays/s1.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from delphi.engine import (
    STOP,
    CompiledProfile,
    Phase,
    compile_profile,
    entropy_bits,
    initial_budgets,
    loss_matrix,
    plan_next,
    risk_by_decision,
    update,
)
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.probes.registry import CLASSIFIER_NAMES

from .test_story_behaviour import ALL_CONSENTS, KAMALA_ANSWERS, KAMALA_PASSIVE, KAMALA_PRIOR

KB_DIR = Path(__file__).resolve().parents[3] / "kb"
AMOUNT_PAISE = 480_000 * 100
VULNERABLE = True
# What Kamala taps (docs/14 §6): the coached "family" purpose, then "No" to the spell-breaker.
TAPS = {"PURPOSE_QUESTION": "FAMILY_OR_FRIEND", "SPELL_BREAKER_DIGITAL_ARREST": "NO"}
TOP_CANDIDATES = 5


def _posterior(profile: CompiledProfile, belief: np.ndarray) -> dict[str, float]:
    return {h: round(float(p), 5) for h, p in zip(profile.hypotheses, belief, strict=True)}


def _question(kb: KnowledgeBase, key: str) -> tuple[str | None, list[dict[str, str]]]:
    entry = kb.question_bank.get(key)
    if entry is None:
        return None, []
    for variant in entry.model_dump().get("variants", []):
        if variant.get("language") == "en":
            options = [{"key": o["key"], "text": o["text"]} for o in variant.get("options", [])]
            return variant.get("text"), options
    return None, []


def build(kb: KnowledgeBase) -> dict[str, Any]:
    profile = compile_profile(kb, "GUARDIAN_PAYMENT")
    belief = np.asarray(KAMALA_PRIOR, dtype=float)
    steps: list[dict[str, Any]] = [
        {
            "kind": "prior",
            "posterior": _posterior(profile, belief),
            "entropy_bits": entropy_bits(belief),
        }
    ]
    observed: list[str] = []
    for key, src in profile.sources.items():  # phase A: passive, run without planning
        if src.kind == "PROBE" and not src.is_active and src.enabled and key in KAMALA_PASSIVE:
            before = belief
            outcome = KAMALA_PASSIVE[key]
            belief = update(belief, src.likelihood[src.outcomes.index(outcome)])
            observed.append(key)
            steps.append(
                {
                    "kind": "passive",
                    "probe_key": key,
                    "outcome": outcome,
                    "posterior": _posterior(profile, belief),
                    "entropy_bits": entropy_bits(belief),
                    "info_bits": entropy_bits(before) - entropy_bits(belief),
                }
            )
    budgets = initial_budgets(profile, tuple(observed))
    losses = loss_matrix(profile, AMOUNT_PAISE, VULNERABLE)
    while True:
        plan = plan_next(
            profile,
            belief,
            amount_paise=AMOUNT_PAISE,
            vulnerable=VULNERABLE,
            phase=Phase.GUARDIAN_ACTIVE,
            observed=frozenset(observed),
            facts=ALL_CONSENTS,
            budgets=budgets,
        )
        if plan.action == STOP:
            steps.append(
                {
                    "kind": "decision",
                    "decision": plan.recommended_decision,
                    "risk_by_decision": {k: round(v) for k, v in plan.risk_by_decision.items()},
                    "posterior": _posterior(profile, belief),
                    "entropy_bits": entropy_bits(belief),
                }
            )
            break
        candidates = sorted(
            (
                {
                    "source_key": c.source_key,
                    "net_value_paise": round(c.net_value_paise),
                    "voi_paise": None if c.voi_myopic_paise is None else round(c.voi_myopic_paise),
                    "cost_paise": round(c.cost_paise),
                    "settle_pct": c.settle_pct,
                }
                for c in plan.candidates
                if c.eligible and c.net_value_paise is not None
            ),
            key=lambda c: -float(c["net_value_paise"] or 0),
        )[:TOP_CANDIDATES]
        key = plan.action
        src = profile.sources[key]
        outcome = KAMALA_ANSWERS[key]
        text, options = _question(kb, key)
        before = belief
        belief = update(belief, src.likelihood[src.outcomes.index(outcome)])
        observed.append(key)
        budgets = budgets.after(src)
        risks = risk_by_decision(belief, losses)
        steps.append(
            {
                "kind": "active",
                "probe_key": key,
                "outcome": outcome,
                "text": text,
                "options": options,
                "tap": TAPS.get(key),
                "candidates": candidates,
                "posterior": _posterior(profile, belief),
                "entropy_bits": entropy_bits(belief),
                "info_bits": entropy_bits(before) - entropy_bits(belief),
                "risk_by_decision": {
                    d: round(float(r)) for d, r in zip(profile.decisions, risks, strict=True)
                },
            }
        )
    return {
        "story": "S-1",
        "title": "Kamala, 68, on a call with “the CBI” at 23:40",
        "persona": {"name": "Kamala Devi", "age": 68, "language": "te", "vulnerable": VULNERABLE},
        "payment": {
            "amount_paise": AMOUNT_PAISE,
            "channel": "UPI_PUSH",
            "payee": "Vikram S. (new payee)",
            "payee_note": "Added 6 min ago",
            "time": "23:40",
        },
        "hypotheses": [
            {
                "key": h,
                "name": kb.hypotheses[h].display_name if h in kb.hypotheses else "Unexplained",
                "color": kb.hypotheses[h].color if h in kb.hypotheses else "#8A8C92",
            }
            for h in profile.hypotheses
        ],
        "source": "tests/golden/test_story_behaviour.py (ENG-S1 fixtures)",
        "steps": steps,
    }


if __name__ == "__main__":
    replay = build(load_and_validate(KB_DIR, CLASSIFIER_NAMES))
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    json.dump(replay, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
