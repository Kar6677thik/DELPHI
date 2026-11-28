"""The key a delivered label is stored under (docs/05 §8, docs/14 §7, #161 review)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from delphi.sim.delivery import stored_key
from delphi.sim.labels import FRAUD_OTHER, LabelSource, ScheduledLabel, SubjectType

KB = frozenset({"GENUINE", "ATO", "APP_DIGITAL_ARREST", "APP_INVESTMENT", "MULE"})


def _label(key: str, source: LabelSource) -> ScheduledLabel:
    return ScheduledLabel(SubjectType.PAYMENT, "p", key, source, datetime(2026, 7, 1, tzinfo=UTC))


@pytest.mark.parametrize("source", list(LabelSource))
def test_a_key_in_the_active_kb_is_kept(source: LabelSource) -> None:
    assert stored_key(_label("ATO", source), KB) == "ATO"


@pytest.mark.parametrize("source", list(LabelSource))
def test_a_key_outside_the_active_kb_is_fraud_other_whatever_the_source(
    source: LabelSource,
) -> None:
    """Only complaints carry hidden keys today; the guard pins it for every source."""
    assert stored_key(_label("PRIZE_ADVANCE_FEE", source), KB) == FRAUD_OTHER
