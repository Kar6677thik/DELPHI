"""Campaigns: the merge rule and stable ids (docs/05 §5, docs/09 §8, FR-NAV-09, #107).

Pure functions over the cases' facts; ``navigator.campaigns.recompute`` (the store, after every
disposition and new case) loads the facts and saves what these return.

**Merge rule.** Two cases are linked when (a) they have the same leading hypothesis (the
posterior's argmax), (b) they share an infrastructure entity (a device fingerprint, a
counterparty account, or a login /24 IPv4 prefix), and (c) they were opened within
``window_days`` (the KB's ``navigator.campaign_window_days``, 7) of each other. A campaign is a
connected component of that relation (union-find closure), so a chain of links joins cases that
are not linked directly. The leading hypothesis is the argmax over all hypotheses; a case led by
GENUINE or UNEXPLAINED is never in a campaign. A case linked to no other case forms no campaign.

**Cost.** Within one (hypothesis, entity) bucket sorted by opening time, only neighbours are
joined (each case with the next, when within the window). Any two cases within the window are
joined by a chain of in-window neighbours, so the groups are the same as joining every pair, in
O(n log n) instead of O(n²) for a busy entity (a shared NAT /24, a mule counterparty).

**Stable ids** (the owner's rule, #259). Each earlier campaign K has an **anchor**: its oldest
member by ``(opened_at, case_id)`` that is still in a group (the oldest member, unless that one
left every campaign). Only the group holding K's anchor may claim K. So when K **splits**, the
group with K's oldest case keeps K and the other groups get new ids (or another campaign they can
claim). When one group can claim several campaigns (they **merged**), the oldest by
``created_at`` (ties: the smaller id) survives and the others are reported as merged into it. A
campaign with no member left in any group is **dissolved**.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from uuid import UUID

from delphi.kb.model import GENUINE, UNEXPLAINED

NEVER_IN_A_CAMPAIGN = frozenset({GENUINE, UNEXPLAINED})  # docs/05 §5


@dataclass(frozen=True, slots=True)
class CaseFacts:
    case_id: UUID
    opened_at: datetime
    leading_hypothesis: str
    entities: frozenset[str]  # "device:<fingerprint>", "account:<id>", "ip24:<prefix>"
    campaign_id: UUID | None = None  # the campaign it was in before this recompute


class _UnionFind:
    def __init__(self, keys: Iterable[UUID]) -> None:
        self.parent = {k: k for k in keys}

    def find(self, k: UUID) -> UUID:
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a: UUID, b: UUID) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb, key=str)] = min(ra, rb, key=str)


def groups(cases: Sequence[CaseFacts], *, window_days: float) -> list[list[UUID]]:
    """The campaigns' members (two or more cases each), each sorted by opening time."""
    eligible = [c for c in cases if c.leading_hypothesis not in NEVER_IN_A_CAMPAIGN]
    uf = _UnionFind(c.case_id for c in eligible)
    window = timedelta(days=window_days)
    by_entity: dict[tuple[str, str], list[CaseFacts]] = {}
    for c in eligible:
        for entity in c.entities:
            by_entity.setdefault((c.leading_hypothesis, entity), []).append(c)
    for members in by_entity.values():
        members.sort(key=lambda c: (c.opened_at, str(c.case_id)))
        for a, b in pairwise(members):
            if b.opened_at - a.opened_at <= window:
                uf.union(a.case_id, b.case_id)
    out: dict[UUID, list[CaseFacts]] = {}
    for c in eligible:
        out.setdefault(uf.find(c.case_id), []).append(c)
    return sorted(
        (
            [c.case_id for c in sorted(g, key=lambda c: (c.opened_at, str(c.case_id)))]
            for g in out.values()
            if len(g) > 1
        ),
        key=lambda ids: str(ids[0]),
    )


@dataclass(frozen=True, slots=True)
class Assignment:
    campaign_of: dict[UUID, UUID]  # case → campaign
    new_campaigns: dict[UUID, list[UUID]]  # a new campaign id → its cases
    merged_into: dict[UUID, UUID]  # a campaign that merged → the one that survived
    dissolved: frozenset[UUID]  # campaigns left with no group (all members unlinked)
    unassigned: frozenset[UUID]  # cases that were in a campaign and are now in none


def assign_ids(
    cases: Sequence[CaseFacts],
    members: Sequence[Sequence[UUID]],
    *,
    created_at: Mapping[UUID, datetime],
    new_id: Callable[[], UUID],
) -> Assignment:
    """Stable ids for ``members`` (from ``groups``), by the anchor rule above. ``created_at``
    gives each existing campaign's creation time.

    Precondition: ``created_at`` holds every campaign a case in ``cases`` points at; a missing
    one is a ``ValueError`` naming it."""
    previous = {c.case_id: c.campaign_id for c in cases if c.campaign_id is not None}
    missing = sorted({str(k) for k in previous.values()} - {str(k) for k in created_at})
    if missing:
        raise ValueError(f"created_at has no time for campaign(s) {', '.join(missing)}")
    group_of = {c: i for i, group in enumerate(members) for c in group}
    anchor_group: dict[UUID, int] = {}  # campaign → the group holding its anchor
    for c in sorted(cases, key=lambda c: (c.opened_at, str(c.case_id))):
        k = c.campaign_id
        if k is not None and k not in anchor_group and c.case_id in group_of:
            anchor_group[k] = group_of[c.case_id]
    claims: dict[int, list[UUID]] = {}
    for k, i in anchor_group.items():
        claims.setdefault(i, []).append(k)
    campaign_of: dict[UUID, UUID] = {}
    new: dict[UUID, list[UUID]] = {}
    merged: dict[UUID, UUID] = {}
    for i, group in enumerate(members):
        olds = sorted(claims.get(i, []), key=lambda k: (created_at[k], str(k)))
        if olds:
            survivor = olds[0]
            for other in olds[1:]:
                merged[other] = survivor
        else:
            survivor = new_id()
            new[survivor] = list(group)
        for c in group:
            campaign_of[c] = survivor
    dissolved = frozenset(set(previous.values()) - set(anchor_group))
    unassigned = frozenset(set(previous) - set(campaign_of))
    return Assignment(campaign_of, new, merged, dissolved, unassigned)
