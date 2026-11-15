"""Applying a Guardian decision to a simulated payment (docs/05 §3).

The payment state machine is driven only by the Guardian session (docs/05 §2). This module owns
the bank side: the status change and, for a release, the execution with its double-entry ledger
(docs/05 §1). In the simulation RELEASED → EXECUTED is immediate, **when the payer can fund
it**. A released payment the payer cannot fund stays RELEASED and unexecuted, with no ledger
entries (docs/05 §3, #94); the session's decision is still RELEASE.

Runs in the caller's transaction, as the app role (the `bank` schema is app read/write,
docs/17 §2). It reads no ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, select, update
from sqlalchemy.orm import Session

from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.sim.bank import accounts, ledger_entries, payments


class PaymentStatus(StrEnum):
    PENDING_GUARDIAN = "PENDING_GUARDIAN"
    RELEASED = "RELEASED"
    EXECUTED = "EXECUTED"
    HELD = "HELD"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"


# docs/05 §2: the decision → the payment status it sets.
STATUS_FOR_DECISION: dict[str, PaymentStatus] = {
    "RELEASE": PaymentStatus.RELEASED,
    "HOLD_FOR_REVIEW": PaymentStatus.HELD,
    "PROTECTIVE_HOLD": PaymentStatus.HELD,
    "DECLINE": PaymentStatus.DECLINED,
}


@dataclass(frozen=True, slots=True)
class PaymentView:
    """What the Guardian needs to know about the payment it decides (no ground truth)."""

    id: UUID
    payer_account_id: UUID
    payee_account_id: UUID
    payer_customer_id: UUID | None
    amount_paise: int
    channel: str
    initiated_at: datetime
    device_id: UUID | None
    status: str


def get_payment(conn: Connection | Session, payment_id: UUID) -> PaymentView | None:
    p, a = payments.c, accounts.c
    row = conn.execute(
        select(
            p.id,
            p.payer_account_id,
            p.payee_account_id,
            a.customer_id,
            p.amount_paise,
            p.channel,
            p.initiated_at,
            p.device_id,
            p.status,
        )
        .join(accounts, accounts.c.id == p.payer_account_id)
        .where(p.id == payment_id)
    ).one_or_none()
    if row is None:
        return None
    return PaymentView(
        id=row.id,
        payer_account_id=row.payer_account_id,
        payee_account_id=row.payee_account_id,
        payer_customer_id=row.customer_id,
        amount_paise=int(row.amount_paise),
        channel=row.channel,
        initiated_at=row.initiated_at,
        device_id=row.device_id,
        status=row.status,
    )


def _move(conn: Connection | Session, account_id: UUID, delta: int) -> int | None:
    """Add ``delta`` to an account's balance unless it would go negative; the new balance."""
    a = accounts.c
    return conn.execute(
        update(accounts)
        .where(a.id == account_id, a.balance_paise + delta >= 0)
        .values(balance_paise=a.balance_paise + delta, version=a.version + 1)
        .returning(a.balance_paise)
    ).scalar_one_or_none()


def apply_guardian_decision(
    conn: Connection | Session, payment_id: UUID, decision: str, at: datetime
) -> PaymentStatus:
    """Move a ``PENDING_GUARDIAN`` payment to the status ``decision`` sets (docs/05 §2–3), and
    execute a funded release. Returns the payment's final status.

    Raises ``NotFoundError`` for an unknown payment, and ``INVALID_STATE_TRANSITION`` if the
    payment is no longer waiting for the Guardian.
    """
    target = STATUS_FOR_DECISION.get(decision)
    if target is None:
        raise DelphiError(f"Unknown decision {decision!r}.", code=ProblemCode.VALIDATION_FAILED)
    return _transition(conn, payment_id, target, at)


def cancel_payment(conn: Connection | Session, payment_id: UUID, at: datetime) -> PaymentStatus:
    """The customer cancelled during a probe (docs/05 §3): ``PENDING_GUARDIAN`` → CANCELLED."""
    return _transition(conn, payment_id, PaymentStatus.CANCELLED, at)


def _transition(
    conn: Connection | Session, payment_id: UUID, target: PaymentStatus, at: datetime
) -> PaymentStatus:
    payment = conn.execute(
        select(payments.c.amount_paise, payments.c.payer_account_id, payments.c.payee_account_id)
        .where(
            payments.c.id == payment_id, payments.c.status == PaymentStatus.PENDING_GUARDIAN.value
        )
        .with_for_update()
    ).one_or_none()
    if payment is None:
        if get_payment(conn, payment_id) is None:
            raise NotFoundError(f"No payment {payment_id}.")
        raise DelphiError(
            f"Payment {payment_id} is not waiting for the Guardian.",
            code=ProblemCode.INVALID_STATE_TRANSITION,
        )
    status, executed_at = target, None
    if target is PaymentStatus.RELEASED and _execute(conn, payment, payment_id, at):
        status, executed_at = PaymentStatus.EXECUTED, at
    conn.execute(
        update(payments)
        .where(payments.c.id == payment_id)
        .values(status=status.value, executed_at=executed_at)
    )
    return status


def _execute(conn: Connection | Session, payment: Any, payment_id: UUID, at: datetime) -> bool:
    """Debit the payer, credit the payee and write both ledger entries, all or nothing. False,
    with nothing written, when the payer cannot fund it."""
    amount = int(payment.amount_paise)
    savepoint = conn.begin_nested()
    payer_after = _move(conn, payment.payer_account_id, -amount)
    payee_after = None if payer_after is None else _move(conn, payment.payee_account_id, amount)
    if payer_after is None or payee_after is None:
        savepoint.rollback()
        return False
    conn.execute(
        ledger_entries.insert(),
        [
            {
                "account_id": payment.payer_account_id,
                "payment_id": payment_id,
                "delta_paise": -amount,
                "balance_after_paise": payer_after,
                "at": at,
            },
            {
                "account_id": payment.payee_account_id,
                "payment_id": payment_id,
                "delta_paise": amount,
                "balance_after_paise": payee_after,
                "at": at,
            },
        ],
    )
    savepoint.commit()
    return True
