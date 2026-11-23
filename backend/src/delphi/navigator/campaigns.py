"""Campaigns: the merge rule and stable ids (docs/05 §5, docs/09 §8, FR-NAV-09, #107).

Pure functions over the cases' facts; ``navigator.campaigns.recompute`` (the store, after every
disposition and new case) loads the facts and saves what these return.

**Merge rule.** Two cases are linked when (a) they have the same leading hypothesis (the
posterior's argmax), (b) they share an infrastructure entity (a device fingerprint, a
counterparty account, or a login /24 IPv4 prefix), and (c) they were opened within
``window_days`` (the KB's ``navigator.campaign_window_days``, 7) of each other. A campaign is a
connected component of that relation (union-find closure), so a chain of links joins cases that
are not linked directly. A case whose
leading hypothesis is GENUINE is never in a campaign; a case linked to no other case forms no
campaign.

**Stable ids.** A group keeps the id its members already had. When a group holds members of
several campaigns (they merged), the oldest campaign's id survives and the others are reported
as merged into it. A group none of whose members had a campaign gets a new id.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

GENUINE = "GENUINE"


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
    eligible = [c for c in cases if c.leading_hypothesis != GENUINE]
    uf = _UnionFind(c.case_id for c in eligible)
    window = timedelta(days=window_days)
    by_entity: dict[tuple[str, str], list[CaseFacts]] = {}
    for c in eligible:
        for entity in c.entities:
            by_entity.setdefault((c.leading_hypothesis, entity), []).append(c)
    for members in by_entity.values():
        members.sort(key=lambda c: (c.opened_at, str(c.case_id)))
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if b.opened_at - a.opened_at > window:
                    break
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


def assign_ids(
    cases: Sequence[CaseFacts],
    members: Sequence[Sequence[UUID]],
    *,
    created_at: Mapping[UUID, datetime],
    new_id: Callable[[], UUID],
) -> Assignment:
    """Stable ids for ``members`` (from ``groups``): ``created_at`` gives each existing
    campaign's creation time, so the oldest survives a merge (ties: the smaller id)."""
    previous = {c.case_id: c.campaign_id for c in cases if c.campaign_id is not None}
    campaign_of: dict[UUID, UUID] = {}
    new: dict[UUID, list[UUID]] = {}
    merged: dict[UUID, UUID] = {}
    used: set[UUID] = set()
    for group in members:
        olds = sorted(
            {previous[c] for c in group if c in previous} - used,
            key=lambda k: (created_at[k], str(k)),
        )
        if olds:
            survivor = olds[0]
            for other in olds[1:]:
                merged[other] = survivor
        else:
            survivor = new_id()
            new[survivor] = list(group)
        used.update(olds)
        for c in group:
            campaign_of[c] = survivor
    dissolved = frozenset(set(previous.values()) - set(campaign_of.values()) - set(merged))
    return Assignment(campaign_of, new, merged, dissolved)
