"""The api's chase candidates (#112): scored with the sensing layer's mule scores."""

from __future__ import annotations

from delphi.chase.candidates import Scored
from delphi.cli import chase_candidates_for
from delphi.sensing.api import MuleScores

from .test_live_guardian_wiring import _platform


def test_the_api_scores_candidates_with_the_mule_scorer() -> None:
    source = chase_candidates_for(_platform())
    assert isinstance(source, Scored)
    assert isinstance(source.mule, MuleScores)
    assert source.demo_time is False
