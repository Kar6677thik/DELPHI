"""Online and offline payment features agree (docs/13 §2, FR-SNS-02): the `test` profile is
written to PostgreSQL, and 500 random customer payments are scored both ways."""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from uuid import UUID

import pytest
from sqlalchemy import select

from delphi.kb.model import KnowledgeBase
from delphi.sensing.config import load_sensing_config
from delphi.sensing.features import build_payment_features, payment_feature_names
from delphi.sensing.history import load_payment_snapshot
from delphi.sensing.offline import OfflineHistories, load_bank_frames
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
