"""The B1 and B2 verdicts a Guardian session shows next to its own decision (docs/08 §6,
docs/22 §2, FR-GDN-15).

They are computed from what the bank knew when the payment was initiated (no ground truth): the
registry listings published by then, when the customer saved the payee and whether a payment to
it had executed before, and when the device was first seen. The inputs are computed as the
history's B1 computes them (``history.py`` ``_decide``), so the monitor never shows a verdict the
history would not have made. B2 uses the Guardian's own prior. Runs as the app
role, in the caller's transaction.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from sqlalchemy import Connection, exists, func, select
from sqlalchemy.orm import Session

from delphi.sim.bank import devices, payees, payments, sim_registry_listings
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.legacy_policy import B1_DECISION, Baseline, decide_b1, decide_b2, payee_is_new
from delphi.sim.payments import PaymentStatus, PaymentView

EXECUTED = PaymentStatus.EXECUTED.value


@lru_cache(maxsize=1)
def _config() -> GeneratorConfig:
    return load_generator_config()


def baseline_decisions(
    conn: Connection | Session,
    payment: PaymentView,
    *,
    p_genuine: float,
    new_device_days: int,
    config: GeneratorConfig | None = None,
) -> dict[str, str]:
    """``{"B1": …, "B2": …}`` for ``payment`` (values of :class:`Baseline`)."""
    cfg = config or _config()
    at = payment.initiated_at
    listed = conn.execute(
        select(
            exists().where(
                sim_registry_listings.c.account_id == payment.payee_account_id,
                sim_registry_listings.c.listed_at <= at,
            )
        )
    ).scalar_one()
    added_at = None
    if payment.payer_customer_id is not None:
        added_at = conn.execute(
            select(func.min(payees.c.added_at)).where(
                payees.c.customer_id == payment.payer_customer_id,
                payees.c.account_id == payment.payee_account_id,
                payees.c.added_at <= at,
            )
        ).scalar_one_or_none()
    # As the history's B1 does (``history.py`` marks a pair paid only once a payment executed):
    # a held, declined or failed attempt is not a payment made.
    ever_paid = conn.execute(
        select(
            exists().where(
                payments.c.payer_account_id == payment.payer_account_id,
                payments.c.payee_account_id == payment.payee_account_id,
                payments.c.status == EXECUTED,
                payments.c.executed_at < at,
            )
        )
    ).scalar_one()
    first_seen = None
    if payment.device_id is not None:
        first_seen = conn.execute(
            select(devices.c.first_seen_at).where(devices.c.id == payment.device_id)
        ).scalar_one_or_none()
    # As the history's B1 does: a payment with no device (or an unknown one) is not "from a new
    # device".
    device_is_new = (
        payment.device_id is not None
        and first_seen is not None
        and at - first_seen < timedelta(days=new_device_days)
    )
    b1 = decide_b1(
        cfg.legacy_policy,
        amount_paise=payment.amount_paise,
        channel=payment.channel,
        payee_listed=bool(listed),
        payee_is_new=payee_is_new(
            cfg.legacy_policy, added_at=added_at, at=at, ever_paid=bool(ever_paid)
        ),
        device_is_new=device_is_new,
    )
    b2 = decide_b2(cfg.score_policy, p_genuine=p_genuine)
    return {"B1": B1_DECISION[b1].value, "B2": b2.value}


__all__ = ["Baseline", "baseline_decisions"]
