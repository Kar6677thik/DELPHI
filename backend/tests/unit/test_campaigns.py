"""Campaigns' merge rule and stable ids (docs/05 §5, FR-NAV-09, #107): merge, chains, GENUINE
and UNEXPLAINED excluded, the split anchor, the 7-day window, and ids that survive recomputes
and merges."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from uuid import UUID

import pytest

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


def _ids() -> Callable[[], UUID]:
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


def test_unexplained_cases_are_never_in_a_campaign() -> None:
    """docs/05 §5: the argmax is over all hypotheses; UNEXPLAINED leads no campaign."""
    cases = [
        _case(1, "UNEXPLAINED", "device:F1"),
        _case(2, "UNEXPLAINED", "device:F1"),
        _case(3, "ATO", "device:F1"),  # shares the device, but no other ATO case does
    ]
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


def test_a_large_bucket_links_by_neighbours_only() -> None:
    """A busy entity: 5,000 same-hypothesis cases on one /24, an hour apart. Joining only
    time-adjacent neighbours gives one campaign, as joining every pair would."""
    cases = [_case(n, "ATO", "ip24:10.0.0", day=n / 24) for n in range(1, 5001)]
    [group] = groups(cases, window_days=WINDOW)
    assert len(group) == 5000
    gap = [_case(1, "ATO", "ip24:10.0.0", day=0), _case(2, "ATO", "ip24:10.0.0", day=7.5)]
    assert groups(gap, window_days=WINDOW) == []  # neighbours more than 7 days apart


def test_a_split_keeps_the_id_for_one_group_and_numbers_the_other() -> None:
    """Two cases leave the link that held the campaign together (the bridge case's entity
    changed): the group holding the oldest case keeps the id, the other gets a new one."""
    stored = [
        _case(1, "ATO", "device:F", campaign=1),
        _case(2, "ATO", "device:F", campaign=1),
        _case(3, "ATO", "account:Y", campaign=1),
        _case(4, "ATO", "account:Y", campaign=1),
    ]
    created = {UUID(int=1001): T0}
    a = assign_ids(stored, groups(stored, window_days=WINDOW), created_at=created, new_id=_ids())
    assert a.campaign_of[UUID(int=1)] == a.campaign_of[UUID(int=2)] == UUID(int=1001)
    [new] = a.new_campaigns
    assert a.campaign_of[UUID(int=3)] == a.campaign_of[UUID(int=4)] == new
    assert a.dissolved == frozenset() and a.unassigned == frozenset()


def test_a_split_keeps_the_id_for_the_group_with_the_oldest_case() -> None:
    """The owner's example on #259: K splits into A (opened on day 0, larger ids) and B (day 1,
    smaller ids). A holds K's oldest case, so A keeps K, though B's ids sort first."""
    stored = [
        _case(9, "ATO", "device:F", day=0, campaign=1),
        _case(10, "ATO", "device:F", day=0, campaign=1),
        _case(1, "ATO", "account:Y", day=1, campaign=1),
        _case(2, "ATO", "account:Y", day=1, campaign=1),
    ]
    members = groups(stored, window_days=WINDOW)
    assert members[0] == [UUID(int=1), UUID(int=2)]  # B sorts first
    a = assign_ids(stored, members, created_at={UUID(int=1001): T0}, new_id=_ids())
    assert a.campaign_of[UUID(int=9)] == a.campaign_of[UUID(int=10)] == UUID(int=1001)
    [new] = a.new_campaigns
    assert a.campaign_of[UUID(int=1)] == a.campaign_of[UUID(int=2)] == new != UUID(int=1001)


def test_when_the_oldest_case_left_the_oldest_still_grouped_anchors() -> None:
    """K's oldest case left every campaign (now GENUINE): K's oldest member still in a group
    anchors it, so that group keeps K."""
    stored = [
        _case(1, "GENUINE", "device:F", day=0, campaign=1),  # the oldest, now released
        _case(5, "ATO", "account:Y", day=2, campaign=1),
        _case(6, "ATO", "account:Y", day=2, campaign=1),
        _case(3, "ATO", "device:G", day=3, campaign=1),
        _case(4, "ATO", "device:G", day=3, campaign=1),
    ]
    a = assign_ids(
        stored, groups(stored, window_days=WINDOW), created_at={UUID(int=1001): T0}, new_id=_ids()
    )
    assert a.campaign_of[UUID(int=5)] == a.campaign_of[UUID(int=6)] == UUID(int=1001)
    assert a.campaign_of[UUID(int=3)] != UUID(int=1001)
    assert a.unassigned == frozenset({UUID(int=1)}) and a.dissolved == frozenset()


def test_a_group_holding_a_split_campaigns_newer_part_cannot_claim_it() -> None:
    """A merge and a split at once: G holds K1's anchor and K2's newer cases; K2's anchor is in
    another group. G keeps K1, and K2 is not merged into it."""
    stored = [
        _case(1, "MULE", "account:A", day=0, campaign=1),
        _case(2, "MULE", "account:A", "account:B", day=1, campaign=2),  # K2's newer case
        _case(3, "MULE", "device:Z", day=0, campaign=2),  # K2's anchor
        _case(4, "MULE", "device:Z", day=0, campaign=2),
    ]
    created = {UUID(int=1001): T0 + timedelta(hours=1), UUID(int=1002): T0}  # K2 is older
    a = assign_ids(stored, groups(stored, window_days=WINDOW), created_at=created, new_id=_ids())
    assert a.campaign_of[UUID(int=1)] == a.campaign_of[UUID(int=2)] == UUID(int=1001)
    assert a.campaign_of[UUID(int=3)] == a.campaign_of[UUID(int=4)] == UUID(int=1002)
    assert a.merged_into == {} and a.new_campaigns == {}


def test_a_campaign_left_with_no_group_is_dissolved_and_its_cases_unassigned() -> None:
    stored = [_case(1, "ATO", "device:F", campaign=1), _case(2, "ATO", "device:G", campaign=1)]
    a = assign_ids(
        stored,
        groups(stored, window_days=WINDOW),
        created_at={UUID(int=1001): T0},
        new_id=_ids(),
    )
    assert a.campaign_of == {} and a.dissolved == frozenset({UUID(int=1001)})
    assert a.unassigned == frozenset({UUID(int=1), UUID(int=2)})


def test_a_three_way_merge_keeps_the_oldest() -> None:
    cases = [
        _case(1, "MULE", "account:A", campaign=1),
        _case(2, "MULE", "account:B", campaign=2),
        _case(3, "MULE", "account:C", campaign=3),
        _case(4, "MULE", "account:A", "account:B", "account:C"),
    ]
    created = {
        UUID(int=1001): T0 + timedelta(hours=2),
        UUID(int=1002): T0,  # the oldest
        UUID(int=1003): T0 + timedelta(hours=1),
    }
    a = assign_ids(cases, groups(cases, window_days=WINDOW), created_at=created, new_id=_ids())
    assert set(a.campaign_of.values()) == {UUID(int=1002)}
    assert a.merged_into == {UUID(int=1001): UUID(int=1002), UUID(int=1003): UUID(int=1002)}


def test_equal_creation_times_keep_the_smaller_id() -> None:
    cases = [
        _case(1, "ATO", "device:F", campaign=7),
        _case(2, "ATO", "device:F", campaign=3),
    ]
    created = {UUID(int=1007): T0, UUID(int=1003): T0}
    a = assign_ids(cases, groups(cases, window_days=WINDOW), created_at=created, new_id=_ids())
    assert set(a.campaign_of.values()) == {UUID(int=1003)}
    assert a.merged_into == {UUID(int=1007): UUID(int=1003)}


def test_a_campaign_the_caller_did_not_load_is_a_clear_error() -> None:
    cases = [_case(1, "ATO", "device:F", campaign=1), _case(2, "ATO", "device:F")]
    with pytest.raises(ValueError, match="no time for campaign"):
        assign_ids(cases, groups(cases, window_days=WINDOW), created_at={}, new_id=_ids())
