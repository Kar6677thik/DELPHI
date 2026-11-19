"""The Navigator evidence adapters (docs/09 §3–4): SQL over the bank as the app role sees it.

Each adapter counts the facts its docs/09 §4 rule needs, relative to the case's ``as_of`` (the
held payment's initiation, or the case's opening for an account review), and returns them with
the observation the rule classifies and the ids of the rows it used. Those facts and ids are
the only material a narrative may cite (docs/09 §4, docs/15). Nothing here reads ground truth.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any
from uuid import UUID

from sqlalchemy import Text, and_, cast, func, select
from sqlalchemy.orm import Session

from delphi.kb.model import ClassifierThresholds
from delphi.probes.evidence import (
    CRM_REPORTS,
    DEVICE_SHARING_WINDOW,
    DWELL_OUT_SHARE,
    DWELL_WINDOW,
    FAN_IN_WINDOW,
    GEO_VELOCITY_WINDOW,
    AccountProfileObs,
    ComplaintsObs,
    CrmObs,
    DeviceSharingObs,
    DwellObs,
    FanInObs,
    GeoVelocityObs,
    PayeeRelationshipObs,
    median_dwell,
)
from delphi.sim import api as bank

EARTH_RADIUS_KM = 6371.0088
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60
EXECUTED = "EXECUTED"


@dataclass(frozen=True, slots=True)
class EvidenceSubject:
    """Who the evidence is about (the KB source's ``subject``): the counterparty account, the
    payer (account and customer), and when."""

    as_of: datetime
    now: datetime  # complaints are "known as of now" (docs/09 §4)
    counterparty_account_id: UUID | None
    payer_account_id: UUID | None
    payer_customer_id: UUID | None


@dataclass(frozen=True, slots=True)
class Fetched:
    observation: Any
    facts: Mapping[str, Any]
    record_refs: Sequence[str] = field(default_factory=tuple)


Adapter = Callable[[Session, EvidenceSubject, ClassifierThresholds], Fetched]


def _days(window: timedelta) -> int:
    return window.days


def _hours(window: timedelta) -> int:
    return int(window.total_seconds() // _SECONDS_PER_HOUR)


def _owner(session: Session, account_id: UUID | None) -> UUID | None:
    if account_id is None:
        return None
    owner: UUID | None = session.execute(
        select(bank.accounts.c.customer_id).where(bank.accounts.c.id == account_id)
    ).scalar_one_or_none()
    return owner


def device_sharing(s: Session, subject: EvidenceSubject, t: ClassifierThresholds) -> Fetched:
    """Other accounts whose owners logged in, in the last 30 days, from a device (by
    fingerprint) the subject account's owner also logged in from."""
    owner = _owner(s, subject.counterparty_account_id)
    since = subject.as_of - DEVICE_SHARING_WINDOW
    if owner is None:  # an external account: the bank sees no logins for it
        return Fetched(
            DeviceSharingObs(0), {"shared_accounts": 0, "window_days": _days(DEVICE_SHARING_WINDOW)}
        )
    le, dv, ac = bank.login_events.c, bank.devices.c, bank.accounts.c
    in_window = and_(le.at >= since, le.at <= subject.as_of)
    fingerprints = (
        select(dv.fingerprint)
        .distinct()
        .join(bank.login_events, le.device_id == dv.id)
        .where(le.customer_id == owner, in_window)
    ).scalar_subquery()
    others = s.execute(
        select(ac.id)
        .distinct()
        .select_from(bank.login_events)
        .join(bank.devices, dv.id == le.device_id)
        .join(bank.accounts, ac.customer_id == le.customer_id)
        .where(
            in_window,
            le.customer_id != owner,
            dv.fingerprint.in_(fingerprints),
            ac.id != subject.counterparty_account_id,
        )
    ).scalars()
    ids = sorted(str(a) for a in others)
    return Fetched(
        DeviceSharingObs(len(ids)),
        {"shared_accounts": len(ids), "window_days": _days(DEVICE_SHARING_WINDOW)},
        ids,
    )


def inbound_fan_in(s: Session, subject: EvidenceSubject, t: ClassifierThresholds) -> Fetched:
    """Distinct payers into the subject account in the last 48 h that never paid it before."""
    if subject.counterparty_account_id is None:
        return Fetched(FanInObs(0), {"unrelated_payers": 0, "window_h": _hours(FAN_IN_WINDOW)})
    p = bank.payments.c
    since = subject.as_of - FAN_IN_WINDOW
    earlier = select(p.payer_account_id).where(
        p.payee_account_id == subject.counterparty_account_id, p.initiated_at < since
    )
    rows = s.execute(
        select(p.payer_account_id, func.min(cast(p.id, Text)))
        .where(
            p.payee_account_id == subject.counterparty_account_id,
            p.initiated_at >= since,
            p.initiated_at <= subject.as_of,
            p.payer_account_id.not_in(earlier),
        )
        .group_by(p.payer_account_id)
    ).all()
    return Fetched(
        FanInObs(len(rows)),
        {"unrelated_payers": len(rows), "window_h": _hours(FAN_IN_WINDOW)},
        sorted(str(r[1]) for r in rows),
    )


def pass_through_dwell(s: Session, subject: EvidenceSubject, t: ClassifierThresholds) -> Fetched:
    """For each credit in the last 7 days, the minutes until 80% of it had left (FIFO, with the
    balance before the window ahead of it in the queue); a credit not yet 80% gone by ``as_of``
    counts at the minutes it has dwelt so far (a lower bound)."""
    if subject.counterparty_account_id is None:
        return Fetched(DwellObs(()), _dwell_facts(DwellObs(())))
    le = bank.ledger_entries.c
    since = subject.as_of - DWELL_WINDOW
    entries = s.execute(
        select(le.id, le.delta_paise, le.balance_after_paise, le.at)
        .where(
            le.account_id == subject.counterparty_account_id,
            le.at >= since,
            le.at <= subject.as_of,
        )
        .order_by(le.at, le.id)
    ).all()
    if not entries:
        return Fetched(DwellObs(()), _dwell_facts(DwellObs(())))
    first = entries[0]
    queue: list[list[Any]] = []  # [remaining, amount, credited_at, index or None]
    opening = first.balance_after_paise - first.delta_paise
    if opening > 0:
        queue.append([opening, opening, None, None])
    minutes: list[float | None] = []
    credited: list[datetime] = []
    refs: list[str] = []
    for e in entries:
        if e.delta_paise > 0:
            queue.append([e.delta_paise, e.delta_paise, e.at, len(minutes)])
            minutes.append(None)
            credited.append(e.at)
            refs.append(str(e.id))
            continue
        out = -e.delta_paise
        while out > 0 and queue:
            head = queue[0]
            take = min(out, head[0])
            head[0] -= take
            out -= take
            index = head[3]
            gone = head[1] - head[0] >= DWELL_OUT_SHARE * head[1]
            if index is not None and minutes[index] is None and gone:
                minutes[index] = (e.at - head[2]).total_seconds() / _SECONDS_PER_MINUTE
            if head[0] == 0:
                queue.pop(0)
    so_far = [
        m if m is not None else (subject.as_of - at).total_seconds() / _SECONDS_PER_MINUTE
        for m, at in zip(minutes, credited, strict=True)
    ]
    obs = DwellObs(tuple(so_far))
    return Fetched(obs, _dwell_facts(obs), refs)


def _dwell_facts(obs: DwellObs) -> dict[str, Any]:
    median = median_dwell(obs)
    return {
        "median_dwell_minutes": None if median is None else round(median, 1),
        "credits": len(obs.minutes_to_out),
        "window_days": _days(DWELL_WINDOW),
        "departure_share": DWELL_OUT_SHARE,
    }


def account_profile(s: Session, subject: EvidenceSubject, t: ClassifierThresholds) -> Fetched:
    ac = bank.accounts.c
    row = s.execute(
        select(ac.id, ac.opened_at, ac.kyc_level).where(ac.id == subject.counterparty_account_id)
    ).one_or_none()
    if row is None:
        return Fetched(
            AccountProfileObs(0.0, "UNKNOWN"), {"account_age_days": 0, "kyc_level": "UNKNOWN"}
        )
    age = (subject.as_of - row.opened_at).total_seconds() / timedelta(days=1).total_seconds()
    return Fetched(
        AccountProfileObs(age, row.kyc_level),
        {"account_age_days": math.floor(age), "kyc_level": row.kyc_level},
        (str(row.id),),
    )


def linked_complaints(s: Session, subject: EvidenceSubject, t: ClassifierThresholds) -> Fetched:
    if subject.counterparty_account_id is None:
        return Fetched(ComplaintsObs(0), {"complaints": 0})
    c = bank.complaints.c
    ids = sorted(
        str(i)
        for i in s.execute(
            select(c.id).where(
                c.counterparty_account_ids.contains([subject.counterparty_account_id]),
                c.reported_at <= subject.now,
            )
        ).scalars()
    )
    return Fetched(ComplaintsObs(len(ids)), {"complaints": len(ids)}, ids)


def crm_contact_history(s: Session, subject: EvidenceSubject, t: ClassifierThresholds) -> Fetched:
    window = {"window_days": t.crm_lookback_days}
    if subject.payer_customer_id is None:
        return Fetched(CrmObs(()), {"event": None, "days_ago": None, **window})
    c = bank.crm_events.c
    since = subject.as_of - timedelta(days=t.crm_lookback_days)
    rows = s.execute(
        select(c.id, c.type, c.at)
        .where(c.customer_id == subject.payer_customer_id, c.at >= since, c.at <= subject.as_of)
        .order_by(c.at)
    ).all()
    reports = [r for r in rows if r.type in CRM_REPORTS]
    latest = reports[-1] if reports else None
    facts = {
        "event": latest.type if latest else None,
        "days_ago": (subject.as_of - latest.at).days if latest else None,
        **window,
    }
    return Fetched(CrmObs(tuple(r.type for r in rows)), facts, [str(r.id) for r in reports])


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def login_geo_velocity(s: Session, subject: EvidenceSubject, t: ClassifierThresholds) -> Fetched:
    """The fastest implied travel between consecutive logins in the last 24 h, and whether the
    last login's city was never seen in the customer's earlier logins (a first login is not a
    new city, docs/13 §3)."""
    window_h = _hours(GEO_VELOCITY_WINDOW)
    nothing = {"last_city": None, "speed_kmh": None, "new_city": False, "window_h": window_h}
    if subject.payer_customer_id is None:
        return Fetched(GeoVelocityObs(None, False), nothing)
    le = bank.login_events.c
    logins = s.execute(
        select(le.id, le.city, le.lat, le.lon, le.at)
        .where(le.customer_id == subject.payer_customer_id, le.at <= subject.as_of)
        .order_by(le.at, le.id)
    ).all()
    if not logins:
        return Fetched(GeoVelocityObs(None, False), nothing)
    recent = [r for r in logins if r.at >= subject.as_of - GEO_VELOCITY_WINDOW]
    speed: float | None = None
    for a, b in pairwise(recent):
        hours = max((b.at - a.at).total_seconds(), 1.0) / _SECONDS_PER_HOUR  # at least 1 s
        implied = _km(a.lat, a.lon, b.lat, b.lon) / hours
        speed = implied if speed is None else max(speed, implied)
    last = logins[-1]
    new_city = len(logins) > 1 and last.city not in {r.city for r in logins[:-1]}
    impossible = speed is not None and speed > t.impossible_travel_kmh
    facts = {  # docs/15 §7: speed_kmh only when it is impossible travel
        "last_city": last.city,
        "speed_kmh": round(speed, 1) if impossible and speed is not None else None,
        "new_city": new_city,
        "window_h": window_h,
    }
    return Fetched(GeoVelocityObs(speed, new_city), facts, [str(r.id) for r in recent])


def payee_relationship(s: Session, subject: EvidenceSubject, t: ClassifierThresholds) -> Fetched:
    if subject.payer_account_id is None or subject.counterparty_account_id is None:
        return Fetched(
            PayeeRelationshipObs(()), {"prior_payments": 0, "max_prior_amount_paise": None}
        )
    p = bank.payments.c
    rows = s.execute(
        select(p.id, p.amount_paise)
        .where(
            p.payer_account_id == subject.payer_account_id,
            p.payee_account_id == subject.counterparty_account_id,
            p.status == EXECUTED,
            p.initiated_at < subject.as_of,
        )
        .order_by(p.initiated_at)
    ).all()
    amounts = tuple(int(r.amount_paise) for r in rows)
    return Fetched(
        PayeeRelationshipObs(amounts),
        {
            "prior_payments": len(amounts),
            "max_prior_amount_paise": max(amounts) if amounts else None,
        },
        [str(r.id) for r in rows],
    )


ADAPTERS: Mapping[str, Adapter] = {
    "BENEFICIARY_DEVICE_SHARING": device_sharing,
    "INBOUND_FAN_IN": inbound_fan_in,
    "PASS_THROUGH_DWELL": pass_through_dwell,
    "ACCOUNT_PROFILE": account_profile,
    "LINKED_COMPLAINTS": linked_complaints,
    "CRM_CONTACT_HISTORY": crm_contact_history,
    "LOGIN_GEO_VELOCITY": login_geo_velocity,
    "PAYEE_RELATIONSHIP": payee_relationship,
}
