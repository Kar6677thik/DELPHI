"""F3 stage indicators for a Scout item on a real database (docs/11 §2, the owner's mapping on
#255): the customer's events in the window before the item's payment, and onward movement from
a home-bank payee after it."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from delphi.scout.f3 import EXECUTION, INITIAL_ACCESS, MONETIZATION, POSITIONING, f3_stages

from .test_cases import Env, env  # noqa: F401 - pytest fixture

WINDOW = timedelta(hours=24)


def _facts(e: Env, payment: UUID) -> Any:
    with e.owner.connect() as conn:
        return conn.execute(
            text(
                "SELECT p.initiated_at, p.payee_account_id, a.customer_id FROM bank.payments p"
                " JOIN bank.accounts a ON a.id = p.payer_account_id WHERE p.id = :p"
            ),
            {"p": payment},
        ).one()


def _stages(e: Env, payment: UUID | None) -> frozenset[str]:
    with e.sessions() as s:
        return f3_stages(s, payment, WINDOW)


def _sql(e: Env, statement: str, **params: object) -> None:
    with e.owner.begin() as conn:
        conn.execute(text(statement), params)


def _login(e: Env, customer: UUID, at: datetime, *, first_seen: datetime) -> None:
    device = uuid4()
    _sql(
        e,
        "INSERT INTO bank.devices (id, customer_id, fingerprint, os, first_seen_at)"
        " VALUES (:d, :c, :f, 'ANDROID', :t)",
        d=device,
        c=customer,
        f=f"fp-{device}",
        t=first_seen,
    )
    _sql(
        e,
        "INSERT INTO bank.login_events (id, customer_id, device_id, ip_prefix24, lat, lon, city,"
        " at) VALUES (:i, :c, :d, '10.0.0.0/24', 19.0, 72.8, 'Mumbai', :t)",
        i=uuid4(),
        c=customer,
        d=device,
        t=at,
    )


def test_an_item_without_a_payment_has_no_stage(env: Env) -> None:  # noqa: F811
    assert _stages(env, None) == frozenset()


def test_the_payment_alone_is_execution(env: Env) -> None:  # noqa: F811
    assert _stages(env, env.payment()) == {EXECUTION}


def test_initial_access_and_positioning_in_the_window(env: Env) -> None:  # noqa: F811
    payment = env.payment()
    at, _payee, customer = _facts(env, payment)
    # Outside the window: a new device a day and a half before, and an old payee.
    _login(env, customer, at - timedelta(hours=36), first_seen=at - timedelta(hours=36))
    _sql(
        env,
        "INSERT INTO bank.payees (id, customer_id, account_id, label, added_at) VALUES"
        " (gen_random_uuid(), :c,"
        " (SELECT payee_account_id FROM bank.payments WHERE id = :p), 'old', :t)"
        " ON CONFLICT DO NOTHING",
        c=customer,
        p=payment,
        t=at - timedelta(days=3),
    )
    assert _stages(env, payment) == {EXECUTION}
    # A login from a long-known device in the window is not initial access.
    _login(env, customer, at - timedelta(hours=2), first_seen=at - timedelta(days=90))
    assert INITIAL_ACCESS not in _stages(env, payment)
    _login(env, customer, at - timedelta(hours=2), first_seen=at - timedelta(hours=3))
    assert _stages(env, payment) == {INITIAL_ACCESS, EXECUTION}
    fresh = uuid4()
    _sql(
        env,
        "INSERT INTO bank.accounts (id, customer_id, bank_code, opened_at, kyc_level, status,"
        " balance_paise) VALUES (:a, NULL, 'XBK1', :t, 'FULL', 'ACTIVE', 0)",
        a=fresh,
        t=at - timedelta(days=400),
    )
    _sql(
        env,
        "INSERT INTO bank.payees (id, customer_id, account_id, label, added_at)"
        " VALUES (gen_random_uuid(), :c, :a, 'new', :t)",
        c=customer,
        a=fresh,
        t=at - timedelta(hours=1),
    )
    assert _stages(env, payment) == {INITIAL_ACCESS, POSITIONING, EXECUTION}


def test_a_sim_swap_or_remote_access_is_initial_access(env: Env) -> None:  # noqa: F811
    payment = env.payment()
    at, _payee, customer = _facts(env, payment)
    _sql(
        env,
        "INSERT INTO bank.crm_events (id, customer_id, type, at)"
        " VALUES (:i, :c, 'DEVICE_LOST_REPORTED', :t)",
        i=uuid4(),
        c=customer,
        t=at - timedelta(hours=1),
    )
    assert INITIAL_ACCESS not in _stages(env, payment)  # a lost device is not mapped
    _sql(
        env,
        "INSERT INTO bank.crm_events (id, customer_id, type, at)"
        " VALUES (:i, :c, 'SIM_SWAP_REPORTED', :t)",
        i=uuid4(),
        c=customer,
        t=at - timedelta(hours=1),
    )
    assert INITIAL_ACCESS in _stages(env, payment)


def test_remote_access_in_the_payment_session_is_initial_access(env: Env) -> None:  # noqa: F811
    payment = env.payment()
    _sql(
        env,
        "INSERT INTO bank.session_telemetry (payment_id, typing_cadence_z, paste_on_account_field,"
        " max_field_dwell_s, remote_access_active, screen_share_active, on_call,"
        " session_duration_s) VALUES (:p, 0, false, 1, false, false, false, 30)"
        " ON CONFLICT (payment_id) DO UPDATE SET remote_access_active = false,"
        " screen_share_active = false",
        p=payment,
    )
    assert INITIAL_ACCESS not in _stages(env, payment)
    _sql(
        env,
        "UPDATE bank.session_telemetry SET screen_share_active = true WHERE payment_id = :p",
        p=payment,
    )
    assert INITIAL_ACCESS in _stages(env, payment)


def test_onward_movement_from_a_home_bank_payee_is_monetization(env: Env) -> None:  # noqa: F811
    payment = env.payment()
    at, payee, _customer = _facts(env, payment)
    _sql(env, "UPDATE bank.accounts SET bank_code = 'DLPH' WHERE id = :a", a=payee)
    onward = uuid4()
    _sql(
        env,
        "INSERT INTO bank.payments (id, payer_account_id, payee_account_id, amount_paise,"
        " channel, initiated_at, status, executed_at, origin) SELECT :i, :a, payer_account_id,"
        " 1000, 'IMPS', :t, 'EXECUTED', :t, origin FROM bank.payments WHERE id = :p",
        i=onward,
        a=payee,
        t=at + timedelta(hours=30),  # after the window: not seen
        p=payment,
    )
    assert MONETIZATION not in _stages(env, payment)
    _sql(
        env,
        "UPDATE bank.payments SET initiated_at = :t, executed_at = :t WHERE id = :i",
        i=onward,
        t=at + timedelta(minutes=20),
    )
    assert MONETIZATION in _stages(env, payment)
    _sql(env, "UPDATE bank.accounts SET bank_code = 'XBK1' WHERE id = :a", a=payee)
    assert MONETIZATION not in _stages(env, payment)  # another bank's account: unobservable
