"""The canonical export of a generated dataset and its SHA-256 (docs/14 §1, FR-SIM-01).

Every table's rows are sorted by their key and serialised as canonical JSON (sorted keys, no
insignificant whitespace, UUIDs and UTC timestamps as strings). Database-assigned columns such
as `created_at` and the ledger's identity id are not generated data, so they are never
exported. Accounts carry their balance at the end of the history, as `bank.accounts` would.
The same profile, seed, config and code give the same bytes on every run and machine.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from delphi.sim.dataset import Dataset
from delphi.sim.rng import quantise

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

Row = dict[str, Any]


def _value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {k: _value(v) for k, v in value.items()}
    if isinstance(value, float):
        return quantise(value)
    return value


def _rows(items: Iterable[DataclassInstance], key: Callable[[Row], Any]) -> list[Row]:
    return sorted(({k: _value(v) for k, v in asdict(item).items()} for item in items), key=key)


def canonical_tables(dataset: Dataset) -> dict[str, list[Row]]:
    pop, network, history = dataset.population, dataset.network, dataset.history
    accounts = [
        replace(a, balance_paise=history.balances[a.id])
        for a in (*pop.accounts, *network.accounts, *history.payroll_accounts)
    ]
    by_id: Callable[[Row], Any] = lambda row: row["id"]  # noqa: E731
    by_account: Callable[[Row], Any] = lambda row: row["account_id"]  # noqa: E731
    # docs/14 §8: the bank sees a listing only once it is made; until then it lives only in
    # sim_truth.label_schedule.
    listed = [r for r in network.listings if r.listed_at < pop.history_end]
    return {
        "customers": _rows((*pop.customers, *network.customers), by_id),
        "accounts": _rows(accounts, by_id),
        "devices": _rows((*pop.devices, *network.devices, *history.devices), by_id),
        "sim_registry_listings": _rows(listed, by_account),
        "account_truth": _rows(dataset.account_truth, by_account),
        "payees": _rows((*pop.payees, *history.payees), by_id),
        "login_events": _rows(history.logins, by_id),
        "payments": _rows(history.payments, by_id),
        "ledger_entries": _rows(
            history.ledger, lambda r: (r["at"], r["payment_id"], r["account_id"])
        ),
        "session_telemetry": _rows(history.telemetry, lambda r: r["payment_id"]),
        "scenario_instances": _rows(history.scenario_instances, by_id),
        "payment_truth": _rows(history.payment_truth, lambda r: r["payment_id"]),
        "label_schedule": _rows(
            (*history.labels, *network.labels),
            lambda r: (r["subject_type"], r["subject_id"], r["label_source"]),
        ),
    }


def canonical_export(dataset: Dataset) -> str:
    pop = dataset.population
    document = {
        "profile": pop.profile,
        "history_start": _value(pop.history_start),
        "history_end": _value(pop.history_end),
        "tables": canonical_tables(dataset),
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def dataset_sha256(dataset: Dataset) -> str:
    return hashlib.sha256(canonical_export(dataset).encode("utf-8")).hexdigest()
