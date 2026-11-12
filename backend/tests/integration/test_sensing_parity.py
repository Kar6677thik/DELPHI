"""Online and offline features agree (docs/13 §2, FR-SNS-02): the `test` profile is written to
PostgreSQL, then 500 random customer payments and 300 random accounts are scored both ways."""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, inspect, select, text

from delphi.kb.model import KnowledgeBase
from delphi.sensing.config import load_sensing_config
from delphi.sensing.features import (
    ACCOUNT_FEATURES,
    build_account_features,
    build_payment_features,
    payment_feature_names,
    to_us,
)
from delphi.sensing.history import (
    complaints_naming,
    load_account_snapshot,
    load_payment_snapshot,
)
from delphi.sensing.offline import OfflineAccountHistories, OfflineHistories, load_bank_frames
from delphi.sim import api as bank
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import load_generator_config
from delphi.sim.writer import write_dataset

from ._db import engine_as, upgrade

SAMPLE = 500
TOLERANCE = 1e-9


def test_online_and_offline_features_agree(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    url = create_database()
    upgrade(url)
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:
        write_dataset(conn, generate_dataset(load_generator_config(), "test", kb=kb))
    sim.dispose()

    config = load_sensing_config().features
    app = engine_as(url, "delphi_app")  # what the api and the worker read the bank as
    try:
        with app.connect() as conn:
            p, a = bank.payments, bank.accounts
            customer_payments = conn.execute(
                select(p.c.id)
                .join(a, a.c.id == p.c.payer_account_id)
                .where(a.c.customer_id.is_not(None))
                .order_by(p.c.id)
            ).scalars()
            ids = random.Random(87).sample(list(customer_payments), SAMPLE)
            offline = OfflineHistories(load_bank_frames(conn), config)
            for payment_id in ids:
                online = build_payment_features(
                    *load_payment_snapshot(conn, payment_id, config), config
                )
                batch = build_payment_features(*offline.snapshot(str(payment_id)), config)
                assert list(online) == list(payment_feature_names(config))
                for name, value in online.items():
                    assert math.isfinite(value), (payment_id, name)
                    assert batch[name] == pytest.approx(value, abs=TOLERANCE), (payment_id, name)
            frame = offline.features(str(i) for i in ids[:5])
            assert list(frame.columns) == list(payment_feature_names(config))
            assert frame.shape == (5, len(payment_feature_names(config)))
    finally:
        app.dispose()


def test_a_payment_without_a_customer_has_no_snapshot(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    url = create_database()
    upgrade(url)
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:
        write_dataset(conn, generate_dataset(load_generator_config(), "test", kb=kb))
    sim.dispose()
    config = load_sensing_config().features
    app = engine_as(url, "delphi_app")
    try:
        with app.connect() as conn:
            p, a = bank.payments, bank.accounts
            salary = conn.execute(
                select(p.c.id)
                .join(a, a.c.id == p.c.payer_account_id)
                .where(a.c.customer_id.is_(None))
                .limit(1)
            ).scalar_one()
            with pytest.raises(LookupError, match="not made by a customer"):
                load_payment_snapshot(conn, salary, config)
            with pytest.raises(LookupError, match="not made by a customer"):
                OfflineHistories(load_bank_frames(conn), config).snapshot(str(salary))
            with pytest.raises(LookupError, match="no payment"):
                load_payment_snapshot(conn, UUID(int=1), config)
    finally:
        app.dispose()


def test_online_and_offline_account_features_agree(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    """docs/13 §6 account features: 300 random accounts (any bank's), each at three times."""
    url = create_database()
    upgrade(url)
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:
        write_dataset(conn, generate_dataset(load_generator_config(), "test", kb=kb))
    sim.dispose()
    config = load_sensing_config().accounts
    app = engine_as(url, "delphi_app")
    try:
        with app.connect() as conn:
            ids = conn.execute(select(bank.accounts.c.id).order_by(bank.accounts.c.id)).scalars()
            sample = random.Random(87).sample(list(ids), 300)
            end = conn.execute(select(func.max(bank.payments.c.initiated_at))).scalar_one()
            offline = OfflineAccountHistories(load_bank_frames(conn), config)
            busy = 0
            for k, account_id in enumerate(sample):
                as_of = end - timedelta(days=(k % 3) * 2, hours=k % 24)
                online = build_account_features(
                    load_account_snapshot(conn, account_id, as_of, config), to_us(as_of), config
                )
                batch = build_account_features(
                    offline.snapshot(str(account_id)), to_us(as_of), config
                )
                assert list(online) == list(ACCOUNT_FEATURES)
                for name, value in online.items():
                    assert math.isfinite(value), (account_id, name)
                    assert batch[name] == pytest.approx(value, abs=TOLERANCE), (account_id, name)
                busy += online["inbound_count_7d"] > 0
            assert busy > 30  # the sample is not all idle accounts
            with pytest.raises(LookupError, match="no account"):
                load_account_snapshot(conn, UUID(int=1), end, config)
            with pytest.raises(LookupError, match="no account"):
                offline.snapshot(str(UUID(int=1)))
            frame = offline.features([str(i) for i in sample[:4]], to_us(end))
            assert list(frame.columns) == list(ACCOUNT_FEATURES) and len(frame) == 4
        indexes = {
            i["name"]
            for table in ("complaints", "devices")
            for i in inspect(app).get_indexes(table, schema="bank")
        }
        assert {"complaints_counterparty_account_ids_idx", "devices_fingerprint_idx"} <= indexes
        # The complaints query must be one the GIN index can serve (`@>`, not `= ANY`). The
        # `test` tables are small, so sequential scans are turned off to see the plan it allows.
        with app.connect() as conn:
            conn.execute(text("SET enable_seqscan = off"))
            query = complaints_naming(sample[0], end).compile(dialect=conn.dialect)
            plan = "\n".join(
                str(line) for (line,) in conn.exec_driver_sql(f"EXPLAIN {query}", query.params)
            )
        assert "complaints_counterparty_account_ids_idx" in plan, plan
    finally:
        app.dispose()


def test_another_customers_device_is_new_on_both_paths(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    """docs/13 §3 (the #140 review): a payment made on a device the bank has long known under
    another customer is on a new device for this customer, online and offline alike."""
    url = create_database()
    upgrade(url)
    sim = engine_as(url, "delphi_sim")
    p, a, d = bank.payments, bank.accounts, bank.devices
    with sim.begin() as conn:
        write_dataset(conn, generate_dataset(load_generator_config(), "test", kb=kb))
        payment, payer, at = conn.execute(
            select(p.c.id, a.c.customer_id, p.c.initiated_at)
            .join(a, a.c.id == p.c.payer_account_id)
            .where(a.c.customer_id.is_not(None), p.c.device_id.is_not(None))
            .order_by(p.c.initiated_at.desc(), p.c.id)
            .limit(1)
        ).one()
        device = conn.execute(  # another customer's device, seen well before the payment
            select(d.c.id)
            .where(d.c.customer_id != payer, d.c.first_seen_at < at - timedelta(days=10))
            .order_by(d.c.id)
            .limit(1)
        ).scalar_one()
        conn.execute(p.update().where(p.c.id == payment).values(device_id=device))
    sim.dispose()

    config = load_sensing_config().features
    app = engine_as(url, "delphi_app")
    try:
        with app.connect() as conn:
            online = build_payment_features(*load_payment_snapshot(conn, payment, config), config)
            offline = OfflineHistories(load_bank_frames(conn), config)
            batch = build_payment_features(*offline.snapshot(str(payment)), config)
    finally:
        app.dispose()
    for features in (online, batch):
        assert features["device_age_days"] == 0.0 and features["is_new_device"] == 1.0
