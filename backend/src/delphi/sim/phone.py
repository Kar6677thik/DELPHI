"""The mock phone's side of the bank (docs/16 "Guardian", docs/08 §1 entry point 1, #98).

``initiate_payment`` is what the customer's app does when they press Pay: a `DEMO` payment,
``PENDING_GUARDIAN``, with the app's session telemetry (what the mobile SDK reports, read by the
DEVICE_POSTURE, SESSION_BEHAVIOUR and CALL_STATUS probes). ``phone_personas`` lists the demo's
customers (``customers.persona_key``, docs/14 §6) with their accounts, saved payees and devices.

Runs in the caller's transaction, as the app role (the `bank` schema is app read/write,
docs/17 §2). It reads no ground truth: the persona manifest in `sim_truth` is never touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args
from uuid import UUID

from sqlalchemy import Connection, select
from sqlalchemy.orm import Session

from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.ids import uuid7
from delphi.sim.bank import accounts, customers, devices, payees, payments, session_telemetry
from delphi.sim.payments import PaymentStatus
from delphi.sim.population import ACTIVE, HOME_BANK

DEMO_ORIGIN = "DEMO"  # docs/05 §1: a payment made from the mock phone
# docs/05 §1: the channels a customer pays on from the app (CASH_OUT is a mule's withdrawal).
PayChannel = Literal["UPI_PUSH", "UPI_COLLECT_APPROVAL", "IMPS", "NEFT"]
PHONE_CHANNELS: tuple[str, ...] = get_args(PayChannel)


@dataclass(frozen=True, slots=True)
class PhoneTelemetry:
    """The app session's signals (`bank.session_telemetry`, docs/08 §5)."""

    typing_cadence_z: float
    paste_on_account_field: bool
    max_field_dwell_s: float
    remote_access_active: bool
    screen_share_active: bool
    on_call: bool
    call_counterparty_known: bool | None
    session_duration_s: float


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    """A payment as the customer's app sends it. The payee is a bank account, or a VPA the
    customer typed (``new_payee``), which the bank resolves to its account."""

    payer_account_id: UUID
    amount_paise: int
    channel: str
    device_id: UUID
    payee_account_id: UUID | None = None
    payee_vpa: str | None = None
    telemetry: PhoneTelemetry | None = None


def _invalid(message: str) -> DelphiError:
    return DelphiError(message, code=ProblemCode.VALIDATION_FAILED)


def _payee(conn: Connection | Session, request: PaymentRequest) -> UUID:
    if (request.payee_account_id is None) == (request.payee_vpa is None):
        raise _invalid("Give exactly one of payee_account_id and new_payee.")
    if request.payee_vpa is not None:
        found = conn.execute(
            select(accounts.c.id).where(accounts.c.vpa == request.payee_vpa)
        ).scalar_one_or_none()
        if found is None:
            raise _invalid("No account has this VPA.")
        return UUID(str(found))
    assert request.payee_account_id is not None
    found = conn.execute(
        select(accounts.c.id).where(accounts.c.id == request.payee_account_id)
    ).scalar_one_or_none()
    if found is None:
        raise _invalid("The payee account does not exist.")
    return request.payee_account_id


def initiate_payment(conn: Connection | Session, request: PaymentRequest, at: datetime) -> UUID:
    """Create the `DEMO` payment, ``PENDING_GUARDIAN``, and its session telemetry. The payer
    must be an active home-bank customer account and the device one of that customer's; the
    payee another account. Anything else is the caller's mistake (422)."""
    if request.channel not in PHONE_CHANNELS:
        raise _invalid(f"channel must be one of {list(PHONE_CHANNELS)}.")
    payer = conn.execute(
        select(accounts.c.customer_id, accounts.c.bank_code, accounts.c.status).where(
            accounts.c.id == request.payer_account_id
        )
    ).one_or_none()
    if payer is None or payer.customer_id is None or payer.bank_code != HOME_BANK:
        raise _invalid("The payer must be a customer's account at this bank.")
    if payer.status != ACTIVE:
        raise _invalid("The payer's account is not active.")
    owner = conn.execute(
        select(devices.c.customer_id).where(devices.c.id == request.device_id)
    ).scalar_one_or_none()
    if owner != payer.customer_id:
        raise _invalid("The device is not one of the payer's.")
    payee = _payee(conn, request)
    if payee == request.payer_account_id:
        raise _invalid("A payment cannot go to the account it comes from.")
    payment_id = uuid7()
    conn.execute(
        payments.insert().values(
            id=payment_id,
            payer_account_id=request.payer_account_id,
            payee_account_id=payee,
            amount_paise=request.amount_paise,
            channel=request.channel,
            initiated_at=at,
            device_id=request.device_id,
            status=PaymentStatus.PENDING_GUARDIAN.value,
            origin=DEMO_ORIGIN,
        )
    )
    if request.telemetry is not None:
        t = request.telemetry
        conn.execute(
            session_telemetry.insert().values(
                payment_id=payment_id,
                typing_cadence_z=t.typing_cadence_z,
                paste_on_account_field=t.paste_on_account_field,
                max_field_dwell_s=t.max_field_dwell_s,
                remote_access_active=t.remote_access_active,
                screen_share_active=t.screen_share_active,
                on_call=t.on_call,
                call_counterparty_known=t.call_counterparty_known,
                session_duration_s=t.session_duration_s,
            )
        )
    return payment_id


# ---------------------------------------------------------------------- personas


@dataclass(frozen=True, slots=True)
class PersonaAccount:
    id: UUID
    label: str
    vpa: str | None
    balance_paise: int


@dataclass(frozen=True, slots=True)
class PersonaPayee:
    account_id: UUID
    label: str
    vpa: str | None
    added_at: datetime


@dataclass(frozen=True, slots=True)
class PersonaDevice:
    id: UUID
    label: str
    os: str
    first_seen_at: datetime


@dataclass(frozen=True, slots=True)
class Persona:
    customer_id: UUID
    persona_key: str
    name: str
    language: str
    vulnerable: bool
    accounts: tuple[PersonaAccount, ...]
    payees: tuple[PersonaPayee, ...]
    devices: tuple[PersonaDevice, ...]


def _grouped[T](rows: list[tuple[UUID, T]]) -> Mapping[UUID, tuple[T, ...]]:
    out: dict[UUID, list[T]] = {}
    for owner, item in rows:
        out.setdefault(owner, []).append(item)
    return {k: tuple(v) for k, v in out.items()}


def phone_personas(conn: Connection | Session) -> list[Persona]:
    """The persona customers in the bank, by persona key; none before a dataset with personas
    is seeded."""
    c, a, p, d = customers.c, accounts.c, payees.c, devices.c
    people = conn.execute(
        select(c.id, c.persona_key, c.display_name, c.language, c.is_vulnerable)
        .where(c.persona_key.is_not(None))
        .order_by(c.persona_key)
    ).all()
    ids = [r.id for r in people]
    if not ids:
        return []
    own = _grouped(
        [
            (r.customer_id, PersonaAccount(r.id, r.vpa or r.bank_code, r.vpa, int(r.balance_paise)))
            for r in conn.execute(
                select(a.id, a.customer_id, a.bank_code, a.vpa, a.balance_paise)
                .where(a.customer_id.in_(ids), a.bank_code == HOME_BANK)
                .order_by(a.opened_at, a.id)
            )
        ]
    )
    saved = _grouped(
        [
            (r.customer_id, PersonaPayee(r.account_id, r.label, r.vpa, r.added_at))
            for r in conn.execute(
                select(p.customer_id, p.account_id, p.label, p.added_at, a.vpa)
                .join(accounts, a.id == p.account_id)
                .where(p.customer_id.in_(ids))
                .order_by(p.added_at, p.id)
            )
        ]
    )
    phones = _grouped(
        [
            (r.customer_id, PersonaDevice(r.id, f"{r.os} phone", r.os, r.first_seen_at))
            for r in conn.execute(
                select(d.id, d.customer_id, d.os, d.first_seen_at)
                .where(d.customer_id.in_(ids))
                .order_by(d.first_seen_at.desc(), d.id)
            )
        ]
    )
    return [
        Persona(
            customer_id=r.id,
            persona_key=r.persona_key,
            name=r.display_name,
            language=r.language,
            vulnerable=r.is_vulnerable,
            accounts=own.get(r.id, ()),
            payees=saved.get(r.id, ()),
            devices=phones.get(r.id, ()),
        )
        for r in people
    ]


def customer_exists(conn: Connection | Session, customer_id: UUID) -> bool:
    found = conn.execute(select(customers.c.id).where(customers.c.id == customer_id)).first()
    return found is not None
