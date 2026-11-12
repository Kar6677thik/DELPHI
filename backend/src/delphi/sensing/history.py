"""The online history snapshot (docs/13 §2): what the bank knew about a payment's customer before
it was initiated, read from PostgreSQL.

It reads the `bank` tables `delphi.sim.api` re-exports (docs/04 §4.1 rule 3), never `sim_truth`,
so it runs as `delphi_app` in the api as well as the worker. Each query is a range scan on an
existing index: payments by (payer_account_id, initiated_at), logins by (customer_id, at), CRM
events by (customer_id, at), payees by (customer_id, account_id), devices and accounts by
customer_id. Only events strictly before the payment's initiation are loaded.
"""

from __future__ import annotations

from uuid import UUID

import numpy as np
from sqlalchemy import Connection, select

from delphi.sensing.config import FeatureConfig
from delphi.sensing.features import CustomerHistory, PaymentView, to_us
from delphi.sim import api as bank


def load_payment_snapshot(
    conn: Connection, payment_id: UUID, config: FeatureConfig
) -> tuple[PaymentView, CustomerHistory]:
    """The payment and its customer's history as of its initiation. ``LookupError`` if the
    payment is unknown or its payer is not a customer of the bank (e.g. a payroll account)."""
    p, t, payer, payee = (
        bank.payments,
        bank.session_telemetry,
        bank.accounts.alias("payer"),
        bank.accounts.alias("payee"),
    )
    row = conn.execute(
        select(
            p,
            payer.c.customer_id,
            payee.c.opened_at.label("payee_opened_at"),
            t.c.session_duration_s,
            t.c.remote_access_active,
        )
        .join(payer, payer.c.id == p.c.payer_account_id)
        .join(payee, payee.c.id == p.c.payee_account_id)
        .outerjoin(t, t.c.payment_id == p.c.id)
        .where(p.c.id == payment_id)
    ).one_or_none()
    if row is None:
        raise LookupError(f"no payment {payment_id}")
    if row.customer_id is None:
        raise LookupError(f"payment {payment_id} is not made by a customer of the bank")
    as_of = row.initiated_at
    customer_id = row.customer_id
    payment = PaymentView(
        id=str(row.id),
        payer_account_id=str(row.payer_account_id),
        payee_account_id=str(row.payee_account_id),
        amount_paise=row.amount_paise,
        channel=row.channel,
        initiated_at_us=to_us(as_of),
        device_id=str(row.device_id) if row.device_id else None,
        payee_account_opened_at_us=to_us(row.payee_opened_at),
        payee_p_mule=config.mule_score_fallback,  # until the mule scorer (#91)
        session_duration_s=row.session_duration_s,
        remote_access_active=row.remote_access_active,
    )

    customer = conn.execute(
        select(bank.customers.c.age, bank.customers.c.is_vulnerable).where(
            bank.customers.c.id == customer_id
        )
    ).one()
    own = select(bank.accounts.c.id).where(bank.accounts.c.customer_id == customer_id)
    payments = conn.execute(
        select(p.c.initiated_at, p.c.amount_paise, p.c.payee_account_id)
        .where(p.c.payer_account_id.in_(own.scalar_subquery()), p.c.initiated_at < as_of)
        .order_by(p.c.initiated_at, p.c.id)
    ).all()
    payees = conn.execute(
        select(bank.payees.c.account_id, bank.payees.c.added_at).where(
            bank.payees.c.customer_id == customer_id, bank.payees.c.added_at < as_of
        )
    ).all()
    d = bank.devices
    devices = conn.execute(
        select(d.c.id, d.c.first_seen_at).where(
            d.c.customer_id == customer_id,  # another customer's device is new to this one
            d.c.first_seen_at < as_of,
        )
    ).all()
    logins = conn.execute(
        select(bank.login_events.c.at, bank.login_events.c.city)
        .where(bank.login_events.c.customer_id == customer_id, bank.login_events.c.at < as_of)
        .order_by(bank.login_events.c.at, bank.login_events.c.id)
    ).all()
    limits = conn.execute(
        select(bank.crm_events.c.at)
        .where(
            bank.crm_events.c.customer_id == customer_id,
            bank.crm_events.c.type.in_(config.limit_increase_event_types),
            bank.crm_events.c.at < as_of,
        )
        .order_by(bank.crm_events.c.at)
    ).all()

    history = CustomerHistory(
        customer_id=str(customer_id),
        age_years=customer.age,
        is_vulnerable=customer.is_vulnerable,
        payment_at_us=np.array([to_us(r.initiated_at) for r in payments], dtype=np.int64),
        payment_amount_paise=np.array([r.amount_paise for r in payments], dtype=np.int64),
        payment_payee=tuple(str(r.payee_account_id) for r in payments),
        payee_added_at_us={str(r.account_id): to_us(r.added_at) for r in payees},
        device_first_seen_us={str(r.id): to_us(r.first_seen_at) for r in devices},
        login_at_us=np.array([to_us(r.at) for r in logins], dtype=np.int64),
        login_city=tuple(r.city for r in logins),
        limit_increase_at_us=np.array([to_us(r.at) for r in limits], dtype=np.int64),
    )
    return payment, history
