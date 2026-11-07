"""Write a generated dataset into PostgreSQL (docs/17 §3, #17, #58).

Every row of ``delphi.sim.export.dataset_records`` is streamed into its ``bank`` or
``sim_truth`` table with ``COPY … FROM STDIN``, one table at a time in foreign-key order,
inside the caller's transaction. Rows go to the server as tuples read straight off the
records, so no table is ever held as dicts (#58).
It runs as ``delphi_sim``, the only role that may write ground truth (docs/17 §2).
Columns the records do not carry (``created_at``, ``version``, the ledger's identity id,
``label_schedule.delivered``) take their database defaults.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import fields
from operator import attrgetter
from typing import Any

from psycopg import Connection as PgConnection
from psycopg import Error as PgError
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy import Connection, Table
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.exc import DBAPIError

from delphi.sim import bank, truth
from delphi.sim.dataset import Dataset
from delphi.sim.export import dataset_records

# Parents before children.
_ORDER: tuple[Table, ...] = (
    bank.customers,
    bank.accounts,
    bank.devices,
    bank.payees,
    bank.sim_registry_listings,
    bank.sim_phone_risk,
    bank.sim_schedule,
    bank.payments,
    bank.ledger_entries,
    bank.session_telemetry,
    bank.login_events,
    bank.crm_events,
    bank.complaints,
    truth.scenario_instances,
    truth.payment_truth,
    truth.account_truth,
    truth.schedule_truth,
    truth.label_schedule,
)

# Enums are StrEnums, which psycopg writes as their value; UUIDs, datetimes, numbers and text
# need nothing either. Only these column types need a conversion.
_Convert = Callable[[Any], Any]


def _jsonb(value: Mapping[str, Any]) -> Jsonb:
    return Jsonb(dict(value))


def _converter(table: Table, column: str) -> _Convert | None:
    kind = table.c[column].type
    if isinstance(kind, JSONB):
        return _jsonb
    if isinstance(kind, ARRAY):
        return list
    return None


def _rows(table: Table, columns: Sequence[str], items: Iterable[Any]) -> Iterable[tuple[Any, ...]]:
    get = attrgetter(*columns)
    converters = [(i, c) for i, c in enumerate(_converter(table, n) for n in columns) if c]
    for item in items:
        row = get(item)
        if len(columns) == 1:
            row = (row,)
        if converters:
            values = list(row)
            for i, convert in converters:
                values[i] = convert(values[i])
            row = tuple(values)
        yield row


def _copy(pg: PgConnection[Any], table: Table, items: Sequence[Any]) -> int:
    if not items:
        return 0
    columns = [f.name for f in fields(items[0])]
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table.schema or "public", table.name),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
    )
    try:
        with pg.cursor() as cursor, cursor.copy(statement) as copy:
            for row in _rows(table, columns, items):
                copy.write_row(row)
    except PgError as error:  # raised as SQLAlchemy's, e.g. IntegrityError, like an insert
        text = statement.as_string(pg)
        raise DBAPIError.instance(text, None, error, PgError) from error
    return len(items)


def write_dataset(conn: Connection, dataset: Dataset) -> Mapping[str, int]:
    """Copy every row of ``dataset`` in; returns the row count per table. The caller owns the
    transaction (``engine.begin()``), so a failure leaves nothing behind."""
    pg = conn.connection.driver_connection
    if not isinstance(pg, PgConnection):
        msg = f"the dataset writer needs a psycopg 3 connection, not {type(pg).__name__}"
        raise TypeError(msg)
    records = dataset_records(dataset)
    return {table.name: _copy(pg, table, records[table.name]) for table in _ORDER}
