"""Police alert drafts for coercion cases (docs/09 §6, FR-NAV-10).

When a COERCION case opens, a police alert is drafted from a template: the victim's pseudonym,
the amount, the beneficiary account, the time, and the evidence list. Its status is DRAFT. Only
an investigator's explicit SEND (recorded as SENT_SIMULATED in the simulation) or DISCARD
changes it. Nothing is ever sent automatically, and a sent or discarded alert never changes
again. Each action is audited (docs/17 §9: police-alert sends).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from delphi.navigator.tables import police_alert_drafts
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.sim import api as bank

_MS = 1000


class AlertStatus(StrEnum):
    DRAFT = "DRAFT"
    SENT_SIMULATED = "SENT_SIMULATED"
    DISCARDED = "DISCARDED"


class AlertAction(StrEnum):
    SEND = "send"
    DISCARD = "discard"


@dataclass(frozen=True, slots=True)
class PoliceAlert:
    id: UUID
    case_id: UUID
    status: AlertStatus
    payload: Mapping[str, Any]
    acted_by: UUID | None
    acted_at: datetime | None

    @staticmethod
    def of(row: Any) -> PoliceAlert:
        return PoliceAlert(
            id=row.id,
            case_id=row.case_id,
            status=AlertStatus(row.status),
            payload=row.payload,
            acted_by=row.acted_by,
            acted_at=row.acted_at,
        )


def pseudonym(customer_id: UUID | None) -> str | None:
    """A stable pseudonym, so the alert names no customer (docs/19)."""
    if customer_id is None:
        return None
    return "C-" + hashlib.sha256(str(customer_id).encode()).hexdigest()[:8].upper()


def draft_alert(session: Session, clock: Clock, *, case_id: UUID, payment_id: UUID) -> PoliceAlert:
    """The template draft for a new coercion case (docs/09 §6 item 2)."""
    payment = bank.get_payment(session, payment_id)
    if payment is None:
        raise NotFoundError(f"No payment {payment_id}.")
    now = clock.now()
    alert_id = uuid7(int(now.timestamp() * _MS))
    payload: dict[str, Any] = {
        "victim": pseudonym(payment.payer_customer_id),
        "amount_paise": payment.amount_paise,
        "channel": payment.channel,
        "beneficiary_account_id": str(payment.payee_account_id),
        "initiated_at": payment.initiated_at.isoformat(),
        "evidence": [],  # filled from the case's evidence when the investigator sends it
    }
    session.execute(
        police_alert_drafts.insert().values(
            id=alert_id,
            case_id=case_id,
            status=AlertStatus.DRAFT.value,
            payload=payload,
            created_at=now,
        )
    )
    alert = get_alert(session, case_id)
    assert alert is not None
    return alert


def get_alert(session: Session, case_id: UUID) -> PoliceAlert | None:
    row = session.execute(
        select(police_alert_drafts).where(police_alert_drafts.c.case_id == case_id)
    ).one_or_none()
    return None if row is None else PoliceAlert.of(row)


def act_on_alert(
    session: Session,
    clock: Clock,
    case_id: UUID,
    action: AlertAction,
    *,
    user_id: UUID,
    evidence: Sequence[Mapping[str, Any]],
) -> PoliceAlert:
    """An investigator's SEND (recorded as SENT_SIMULATED, with the case's evidence list at that
    moment) or DISCARD of the case's draft. Only a DRAFT can be acted on."""
    pa = police_alert_drafts.c
    row = session.execute(
        select(police_alert_drafts).where(pa.case_id == case_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise NotFoundError("This case has no police alert (only coercion cases do).")
    if row.status != AlertStatus.DRAFT.value:
        raise DelphiError(
            f"The police alert is already {row.status}.",
            code=ProblemCode.INVALID_STATE_TRANSITION,
        )
    now = clock.now()
    status = AlertStatus.SENT_SIMULATED if action is AlertAction.SEND else AlertStatus.DISCARDED
    payload = dict(row.payload)
    if action is AlertAction.SEND:
        payload["evidence"] = list(evidence)
    session.execute(
        update(police_alert_drafts)
        .where(pa.id == row.id)
        .values(status=status.value, payload=payload, acted_by=user_id, acted_at=now)
    )
    record(
        session,
        clock,
        actor_type=ActorType.USER,
        actor_id=str(user_id),
        action=f"police_alert.{'sent' if action is AlertAction.SEND else 'discarded'}",
        entity_type="case",
        entity_id=str(case_id),
        payload={"police_alert_id": str(row.id), "status": status.value},
    )
    publish(session, EventType.CASE_UPDATED, str(case_id), {"case_id": str(case_id)})
    alert = get_alert(session, case_id)
    assert alert is not None
    return alert
