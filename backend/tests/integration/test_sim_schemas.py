"""The simulated bank and ground-truth tables, and who may read them (docs/17 §2–3, FR-SIM-07)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, Table, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from delphi.sim import bank, truth

from ._db import engine_as

TRUTH_TABLES = sorted(truth.metadata.tables.values(), key=lambda t: t.name)
BANK_TABLES = sorted(bank.metadata.tables.values(), key=lambda t: t.name)
T0 = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_url: str) -> Iterator[Engine]:
    engine = engine_as(migrated_url, "delphi_app")
    yield engine
    engine.dispose()


@pytest.fixture
def sim_engine(migrated_url: str) -> Iterator[Engine]:
    engine = engine_as(migrated_url, "delphi_sim")
    yield engine
    engine.dispose()


def test_head_creates_every_bank_and_truth_table(owner_engine: Engine) -> None:
    inspector = inspect(owner_engine)
    assert set(inspector.get_table_names(schema="bank")) == {t.name for t in BANK_TABLES}
    assert set(inspector.get_table_names(schema="sim_truth")) == {t.name for t in TRUTH_TABLES}


@pytest.mark.parametrize("table", [*BANK_TABLES, *TRUTH_TABLES], ids=lambda t: t.fullname)
def test_core_definitions_match_the_migrated_schema(owner_engine: Engine, table: Table) -> None:
    """The generator writes through these definitions, so they must not drift from 0002."""
    inspector = inspect(owner_engine)
    reflected = {c["name"]: c for c in inspector.get_columns(table.name, schema=table.schema)}
    assert list(reflected) == [c.name for c in table.columns]
    for column in table.columns:
        assert reflected[column.name]["nullable"] == column.nullable, column.name
    primary_key = inspector.get_pk_constraint(table.name, schema=table.schema)
    assert primary_key["constrained_columns"] == [c.name for c in table.primary_key]


@pytest.mark.parametrize("table", TRUTH_TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize(
    "statement",
    ["SELECT * FROM {t}", "DELETE FROM {t}", "INSERT INTO {t} DEFAULT VALUES"],
    ids=["select", "delete", "insert"],
)
def test_the_app_role_cannot_touch_ground_truth(
    app_engine: Engine, table: Table, statement: str
) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"), app_engine.connect() as conn:
        conn.execute(text(statement.format(t=table.fullname)))


@pytest.mark.parametrize("table", TRUTH_TABLES, ids=lambda t: t.name)
def test_the_app_role_has_no_privilege_on_any_truth_table(
    owner_engine: Engine, table: Table
) -> None:
    with owner_engine.connect() as conn:
        granted = conn.execute(
            text(
                "SELECT has_table_privilege('delphi_app', :t, "
                "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')"
            ),
            {"t": table.fullname},
        ).scalar_one()
    assert granted is False


@pytest.mark.parametrize("table", TRUTH_TABLES, ids=lambda t: t.name)
def test_the_sim_role_may_read_and_write_ground_truth(sim_engine: Engine, table: Table) -> None:
    with sim_engine.connect() as conn:
        conn.execute(text(f"SELECT * FROM {table.fullname}"))
        conn.execute(text(f"DELETE FROM {table.fullname} WHERE false"))
        conn.rollback()


def _customer_and_accounts(conn: object) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    from sqlalchemy import Connection

    assert isinstance(conn, Connection)
    customer, payer, payee = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute(
        bank.customers.insert().values(
            id=customer,
            display_name="Test",
            age=40,
            language="en",
            is_vulnerable=False,
            disability_flag=False,
            segment="RETAIL",
            income_band="MID",
            consent_call_status=True,
            home_city="Hyderabad",
            created_at=T0,
        )
    )
    for account, owner in ((payer, customer), (payee, None)):
        conn.execute(
            bank.accounts.insert().values(
                id=account,
                customer_id=owner,
                bank_code="DLPH" if owner else "XBK2",
                is_merchant=False,
                opened_at=T0,
                kyc_level="FULL",
                status="ACTIVE",
                balance_paise=1_000_000,
                created_at=T0,
                version=1,
            )
        )
    return customer, payer, payee


def _payment(payer: uuid.UUID, payee: uuid.UUID, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "payer_account_id": payer,
        "payee_account_id": payee,
        "amount_paise": 50_000,
        "channel": "UPI_PUSH",
        "initiated_at": T0,
        "status": "EXECUTED",
        "origin": "SIM_HISTORY",
        "created_at": T0,
    }
    return {**values, **overrides}


def test_the_app_role_has_dml_on_the_bank(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        _, payer, payee = _customer_and_accounts(conn)
        conn.execute(bank.payments.insert().values(**_payment(payer, payee)))
        conn.rollback()


@pytest.mark.parametrize(
    "overrides",
    [{"channel": "CARRIER_PIGEON"}, {"amount_paise": 0}, {"status": "LOST"}, {"origin": "X"}],
    ids=["channel", "amount", "status", "origin"],
)
def test_bank_checks_reject_bad_values(app_engine: Engine, overrides: dict[str, object]) -> None:
    with pytest.raises(IntegrityError), app_engine.connect() as conn:
        _, payer, payee = _customer_and_accounts(conn)
        conn.execute(bank.payments.insert().values(**_payment(payer, payee, **overrides)))


def test_truth_checks_reject_bad_values(sim_engine: Engine) -> None:
    with pytest.raises(IntegrityError), sim_engine.connect() as conn:
        conn.execute(
            truth.label_schedule.insert().values(
                subject_type="PAYMENT",
                subject_id=uuid.uuid4(),
                hypothesis_key="ATO",
                label_source="RUMOUR",
                due_at=T0,
                delivered=False,
                created_at=T0,
            )
        )


def test_truth_protects_the_bank_rows_it_describes(sim_engine: Engine) -> None:
    """Deleting a payment that ground truth still describes fails; truth is never lost
    silently (NO ACTION foreign keys)."""
    with sim_engine.connect() as conn:
        _, payer, payee = _customer_and_accounts(conn)
        payment = _payment(payer, payee)
        conn.execute(bank.payments.insert().values(**payment))
        instance = uuid.uuid4()
        conn.execute(
            truth.scenario_instances.insert().values(
                id=instance,
                scenario_type="ATO",
                hypothesis_key="ATO",
                params={},
                started_at=T0,
                created_at=T0,
            )
        )
        conn.execute(
            truth.payment_truth.insert().values(
                payment_id=payment["id"],
                scenario_instance_id=instance,
                is_fraud=True,
                hypothesis_key="ATO",
                coached=False,
                created_at=T0,
            )
        )
        with pytest.raises(IntegrityError, match="payment_truth"):
            conn.execute(bank.payments.delete().where(bank.payments.c.id == payment["id"]))
        conn.rollback()
