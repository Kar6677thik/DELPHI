"""Writing a generated dataset into PostgreSQL (docs/17 §2–3, #17)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from delphi.kb.model import KnowledgeBase
from delphi.sim import bank, truth
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.export import dataset_records
from delphi.sim.generator_config import load_generator_config
from delphi.sim.writer import write_dataset

from ._db import engine_as, upgrade


@dataclass(frozen=True)
class Written:
    url: str
    dataset: Dataset
    counts: Mapping[str, int]


@pytest.fixture(scope="module")
def written(create_database: Callable[[], str], kb: KnowledgeBase) -> Written:
    """The `test` profile, written once as delphi_sim into a fresh database at head."""
    url = create_database()
    upgrade(url)
    dataset = generate_dataset(load_generator_config(), "test", kb=kb)
    engine = engine_as(url, "delphi_sim")
    with engine.begin() as conn:
        counts = write_dataset(conn, dataset)
    engine.dispose()
    return Written(url, dataset, counts)


def test_every_row_is_written(written: Written) -> None:
    records = dataset_records(written.dataset)
    engine = engine_as(written.url)
    with engine.connect() as conn:
        for table in (*bank.metadata.tables.values(), *truth.metadata.tables.values()):
            stored = conn.execute(text(f"SELECT count(*) FROM {table.fullname}")).scalar_one()
            expected = len(records.get(table.name, []))
            assert stored == expected == written.counts.get(table.name, 0), table.fullname
        undelivered = conn.execute(
            text("SELECT count(*) FROM sim_truth.label_schedule WHERE NOT delivered")
        ).scalar_one()
        live = conn.execute(
            text("SELECT count(*) FROM bank.payments WHERE origin = 'SIM_LIVE'")
        ).scalar_one()
    engine.dispose()
    assert undelivered == len(records["label_schedule"]) > 0
    window = written.dataset.history.window
    assert window is not None and live == len(window.payments) > 0


def test_the_app_role_reads_the_bank_but_not_the_truth(written: Written) -> None:
    engine = engine_as(written.url, "delphi_app")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM bank.payments")).scalar_one() > 0
    with pytest.raises(ProgrammingError, match="permission denied"), engine.connect() as conn:
        conn.execute(text("SELECT count(*) FROM sim_truth.payment_truth"))
    engine.dispose()


def test_a_second_write_fails_and_leaves_nothing_behind(written: Written) -> None:
    engine = engine_as(written.url, "delphi_sim")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        write_dataset(conn, written.dataset)
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT count(*) FROM bank.payments")).scalar_one()
    engine.dispose()
    assert stored == written.counts["payments"]


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        ("UPDATE bank.customers SET segment = 'PRIVATE'", "customers_segment_check"),
        ("UPDATE bank.devices SET os = 'SYMBIAN'", "devices_os_check"),
        (
            "UPDATE bank.accounts SET merchant_category = 'CASINO' WHERE is_merchant",
            "accounts_merchant_category_check",
        ),
        ("UPDATE bank.crm_events SET type = 'CHAT'", "crm_events_type_check"),
        ("UPDATE bank.complaints SET category = 'OTHER'", "complaints_category_check"),
    ],
)
def test_migration_0003_checks_the_fixed_value_sets(
    written: Written, statement: str, constraint: str
) -> None:
    engine = engine_as(written.url)
    with pytest.raises(IntegrityError, match=constraint), engine.begin() as conn:
        conn.execute(text(statement))
    engine.dispose()


def test_a_null_merchant_category_is_still_allowed(written: Written) -> None:
    engine = engine_as(written.url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bank.accounts (id, bank_code, kyc_level, status, balance_paise, "
                "opened_at) VALUES (:id, 'XBK1', 'FULL', 'ACTIVE', 0, now())"
            ),
            {"id": uuid.uuid4()},
        )
        conn.rollback()
    engine.dispose()
