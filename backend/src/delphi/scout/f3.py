"""F3 stage indicators for a Scout item (docs/11 §2, the owner's mapping on #255).

The customer's events in the ``window`` (the KB's ``scout.f3_window_h``, 24 h) before the
item's payment, mapped to the docs/02 F3 stages, using the event types the simulator emits:

- ``INITIAL_ACCESS``: a login from a device first seen in the window, a SIM swap reported to
  the bank (``crm_events`` ``SIM_SWAP_REPORTED``), or remote access or screen sharing active in
  a payment session of the customer's in the window (the item's own included).
- ``POSITIONING``: a payee added in the window. The simulator emits no limit or profile/contact
  changes, so those cannot be seen yet.
- ``EXECUTION``: an outgoing payment; every item with a payment has its own.
- ``MONETIZATION``: onward movement from the payee, which the customer's bank sees only when the
  payee is one of its own accounts: an executed payment out of a home-bank payee account in the
  window after the item's payment. Otherwise it is not set.

An item without a payment (an account review) has no customer events to read: no stage.
``DEVICE_LOST_REPORTED`` is not mapped: a lost device is neither a SIM nor a credential change.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from delphi.sim import api as bank

INITIAL_ACCESS = "INITIAL_ACCESS"
POSITIONING = "POSITIONING"
EXECUTION = "EXECUTION"
MONETIZATION = "MONETIZATION"
STAGES = (INITIAL_ACCESS, POSITIONING, EXECUTION, MONETIZATION)  # docs/02 order
SIM_SWAP = "SIM_SWAP_REPORTED"
EXECUTED = "EXECUTED"


def f3_stages(session: Session, payment_id: UUID | None, window: timedelta) -> frozenset[str]:
    """The F3 stages the customer's events show around the item's payment (see above)."""
    if payment_id is None:
        return frozenset()
    p, a = bank.payments.c, bank.accounts.c
    row = session.execute(
        select(p.initiated_at, p.payee_account_id, a.customer_id)
        .join(bank.accounts, a.id == p.payer_account_id)
        .where(p.id == payment_id)
    ).one_or_none()
    if row is None:
        return frozenset()
    at, customer = row.initiated_at, row.customer_id
    since = at - window
    stages = {EXECUTION}
    if customer is not None and _initial_access(session, customer, since, at):
        stages.add(INITIAL_ACCESS)
    if customer is not None and _positioning(session, customer, since, at):
        stages.add(POSITIONING)
    if _monetization(session, row.payee_account_id, at, at + window):
        stages.add(MONETIZATION)
    return frozenset(stages)


def _initial_access(session: Session, customer: UUID, since: object, at: object) -> bool:
    login, device, crm = bank.login_events.c, bank.devices.c, bank.crm_events.c
    p, a, t = bank.payments.c, bank.accounts.c, bank.session_telemetry.c
    new_device = (
        exists()
        .where(login.customer_id == customer, login.at > since, login.at <= at)
        .where(device.id == login.device_id, device.first_seen_at > since)
    )
    sim_swap = exists().where(
        crm.customer_id == customer, crm.type == SIM_SWAP, crm.at > since, crm.at <= at
    )
    remote = (
        exists()
        .where(t.payment_id == p.id, or_(t.remote_access_active, t.screen_share_active))
        .where(p.payer_account_id == a.id, a.customer_id == customer)
        .where(p.initiated_at > since, p.initiated_at <= at)
    )
    return bool(session.execute(select(or_(new_device, sim_swap, remote))).scalar())


def _positioning(session: Session, customer: UUID, since: object, at: object) -> bool:
    payee = bank.payees.c
    added = exists().where(
        payee.customer_id == customer, payee.added_at > since, payee.added_at <= at
    )
    return bool(session.execute(select(added)).scalar())


def _monetization(session: Session, payee_account: UUID | None, at: object, until: object) -> bool:
    if payee_account is None:
        return False
    a, p = bank.accounts.c, bank.payments.c
    onward = exists().where(
        and_(
            a.id == payee_account,
            a.bank_code == bank.HOME_BANK,
            p.payer_account_id == payee_account,
            p.status == EXECUTED,
            # A payment executed by ``until`` was initiated by then too: this bound lets the
            # (payer_account_id, initiated_at) index limit the scan (#298 review).
            p.initiated_at <= until,
            p.executed_at > at,
            p.executed_at <= until,
        )
    )
    return bool(session.execute(select(onward)).scalar())
