"""Write a generated dataset into PostgreSQL (docs/17 §3, #17).

Every row of ``delphi.sim.export.dataset_records`` goes through the ``delphi.sim.bank`` and
``delphi.sim.truth`` Core tables, in foreign-key order, in chunks, inside one transaction.
It runs as ``delphi_sim``, the only role that may write ground truth (docs/17 §2).
Database-assigned columns (``created_at``, ``version``, the ledger's identity id) take their
defaults.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Connection, Table

from delphi.sim import bank, truth
from delphi.sim.dataset import Dataset
from delphi.sim.export import dataset_records

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

CHUNK_ROWS = 5_000

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
    truth.label_schedule,
)


def _column_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    return value


def _row(table: Table, item: DataclassInstance) -> dict[str, Any]:
    row = {k: _column_value(v) for k, v in asdict(item).items()}
    if table is truth.label_schedule:
        row["subject_id"] = UUID(str(row["subject_id"]))
        row["delivered"] = False
    return row


def _chunks(rows: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), CHUNK_ROWS):
        yield rows[start : start + CHUNK_ROWS]


def write_dataset(conn: Connection, dataset: Dataset) -> Mapping[str, int]:
    """Insert every row of ``dataset``; returns the row count per table. The caller owns the
    transaction (``engine.begin()``), so a failure leaves nothing behind."""
    records = dataset_records(dataset)
    counts: dict[str, int] = {}
    for table in _ORDER:
        rows = [_row(table, item) for item in records[table.name]]
        for chunk in _chunks(rows):
            conn.execute(table.insert(), chunk)
        counts[table.name] = len(rows)
    return counts
