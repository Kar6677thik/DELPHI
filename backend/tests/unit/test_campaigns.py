"""Campaigns' merge rule and stable ids (docs/05 §5, FR-NAV-09, #107): merge, chains, GENUINE
excluded, the 7-day window, and ids that survive recomputes and merges."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from uuid import UUID

from delphi.navigator.campaigns import CaseFacts, assign_ids, groups

T0 = datetime(2026, 11, 1, tzinfo=UTC)
WINDOW = 7


def _case(
    n: int, hyp: str, *entities: str, day: float = 0, campaign: int | None = None
) -> CaseFacts:
    return CaseFacts(
        UUID(int=n),
        T0 + timedelta(days=day),
        hyp,
        frozenset(entities),
        UUID(int=1000 + campaign) if campaign is not None else None,
    )


def _ids() -> object:
    numbers = count(5000)
    return lambda: UUID(int=next(numbers))


def test_a_shared_entity_and_the_same_hypothesis_merge() -> None:
    cases = [
        _case(1, "APP_DIGITAL_ARREST", "device:F1"),
        _case(2, "APP_DIGITAL_ARREST", "device:F1", "account:A"),
        _case(3, "APP_INVESTMENT", "device:F1"),  # another hypothesis: not linked
        _case(4, "APP_DIGITAL_ARREST", "account:B"),  # nothing shared: no campaign
    ]
    assert groups(cases, window_days=WINDOW) == [[UUID(int=1), UUID(int=2)]]


def test_chains_join_cases_that_share_nothing_directly() -> None:
    cases = [
        _case(1, "ATO", "device:F1"),
        _case(2, "ATO", "device:F1", "ip24:10.0.0"),
        _case(3, "ATO", "ip24:10.0.0", "account:X"),
        _case(4, "ATO", "account:X"),
    ]
    assert groups(cases, window_days=WINDOW) == [[UUID(int=n) for n in (1, 2, 3, 4)]]


def test_genuine_cases_are_never_in_a_campaign() -> None:
    cases = [_case(1, "GENUINE", "device:F1"), _case(2, "GENUINE", "device:F1")]
    assert groups(cases, window_days=WINDOW) == []


def test_cases_opened_more_than_seven_days_apart_are_not_linked() -> None:
    cases = [_case(1, "MULE", "account:M", day=0), _case(2, "MULE", "account:M", day=8)]
    assert groups(cases, window_days=WINDOW) == []
    bridged = [*cases, _case(3, "MULE", "account:M", day=4)]  # within 7 days of both
    assert groups(bridged, window_days=WINDOW) == [[UUID(int=n) for n in (1, 3, 2)]]


def test_ids_are_stable_and_the_oldest_survives_a_merge() -> None:
    first = [_case(1, "ATO", "device:F"), _case(2, "ATO", "device:F")]
    a = assign_ids(first, groups(first, window_days=WINDOW), created_at={}, new_id=_ids())
    [campaign] = a.new_campaigns
    assert a.campaign_of == {UUID(int=1): campaign, UUID(int=2): campaign}

    # Recompute with the stored ids: nothing changes.
    stored = [_case(1, "ATO", "device:F", campaign=1), _case(2, "ATO", "device:F", campaign=1)]
    created = {UUID(int=1001): T0, UUID(int=1002): T0 + timedelta(hours=1)}
    again = assign_ids(
        stored, groups(stored, window_days=WINDOW), created_at=created, new_id=_ids()
    )
    assert set(again.campaign_of.values()) == {UUID(int=1001)} and again.new_campaigns == {}

    # A new case bridges campaign 1001 and the younger 1002: 1001 survives.
    merging = [
        *stored,
        _case(3, "ATO", "account:Y", campaign=2),
        _case(4, "ATO", "account:Y", campaign=2),
        _case(5, "ATO", "device:F", "account:Y"),
    ]
    m = assign_ids(merging, groups(merging, window_days=WINDOW), created_at=created, new_id=_ids())
    assert set(m.campaign_of.values()) == {UUID(int=1001)}
    assert m.merged_into == {UUID(int=1002): UUID(int=1001)}
    assert m.dissolved == frozenset()
