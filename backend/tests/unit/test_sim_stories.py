"""The DEMO_STORY runner's parameters and scripts (docs/14 §6, §10, #101), and the worker's
Guardian slot before it starts (#95)."""

from __future__ import annotations

from typing import Any

import pytest

from delphi.guardian.autoplay import WorkerGuardian
from delphi.sim.customer import AnswerStep
from delphi.sim.persona_config import StoryAnswer, load_persona_config
from delphi.sim.scenarios import ScenarioType
from delphi.sim.stories import StoryParams, scripted_steps


def test_only_s1_and_s2_run_in_either_mode() -> None:
    assert StoryParams.of({"story": "S2", "mode": "autoplay"}) == StoryParams("S2", "autoplay")
    for params, message in (
        ({"story": "S3", "mode": "autoplay"}, "story must be"),
        ({"story": "S1", "mode": "replay"}, "mode must be"),
        ({}, "story must be"),
    ):
        with pytest.raises(ValueError, match=message):
            StoryParams.of(params)


def test_the_scripts_become_the_answers_the_phone_would_send() -> None:
    """docs/08 §4 bodies, with the scripted delays and app switches (docs/14 §6)."""
    stories = load_persona_config().stories
    assert scripted_steps(stories["S1"].answers) == {
        "PURPOSE_QUESTION": [AnswerStep({"option_key": "FAMILY_OR_FRIEND"}, 6.0, 0)],
        "SPELL_BREAKER_DIGITAL_ARREST": [AnswerStep({"option_key": "NO"}, 11.0, 1)],
    }
    assert scripted_steps(stories["S2"].answers)["BIOMETRIC_STEP_UP"] == [
        AnswerStep({"biometric": "MATCH"}, 2.0, 0)
    ]
    with pytest.raises(ValueError, match="cannot script"):
        scripted_steps([StoryAnswer(probe="OTP_STEP_UP", answer="123456", after_s=1)])


def test_the_runnable_stories_say_what_their_payment_enacts() -> None:
    stories = load_persona_config().stories
    s1, s2 = stories["S1"], stories["S2"]
    assert (s1.scenario, s1.coached) == (ScenarioType.APP_DIGITAL_ARREST, True)
    assert (s2.scenario, s2.coached) == (ScenarioType.GENUINE_UNUSUAL, False)


def test_the_worker_slot_refuses_work_before_it_starts() -> None:
    unused: Any = None
    slot = WorkerGuardian(runner=unused, channel=unused, sessions=unused)

    async def nothing() -> None:
        return None

    with pytest.raises(RuntimeError, match="not started"):
        slot._call(nothing())
    slot.stop()  # never started: nothing to stop
