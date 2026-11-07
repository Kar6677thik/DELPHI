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
    if isinstance(value, tuple | list):
        return [_value(v) for v in value]
    return value


def _rows(items: Iterable[DataclassInstance], key: Callable[[Row], Any]) -> list[Row]:
    return sorted(({k: _value(v) for k, v in asdict(item).items()} for item in items), key=key)


def dataset_records(dataset: Dataset) -> dict[str, list[DataclassInstance]]:
    """Every generated row, per `bank` / `sim_truth` table, in no particular order. Accounts
    carry their end-of-history balances; the live/eval window's undecided sessions join the
    history's payments, telemetry, logins and truth."""
    pop, network, history = dataset.population, dataset.network, dataset.history
    window = history.window
    accounts = [
        replace(a, balance_paise=history.balances[a.id])
        for a in (
            *pop.accounts,
            *network.accounts,
            *history.payroll_accounts,
            *history.cash_out_accounts,
        )
    ]
    # docs/14 §8: the bank sees a listing only once it is made; until then it lives only in
    # sim_truth.label_schedule.
    listed = [
        r for r in (*network.listings, *history.registry_listings) if r.listed_at < pop.history_end
    ]
    return {
        "customers": [*pop.customers, *network.customers],
        "accounts": list(accounts),
        "devices": [*pop.devices, *network.devices, *history.devices],
        "payees": [*pop.payees, *history.payees],
        "sim_registry_listings": list(listed),
        "sim_phone_risk": list(dataset.phone_risk),
        "sim_schedule": list(history.schedule),
        "payments": [*history.payments, *(window.payments if window else ())],
        "ledger_entries": list(history.ledger),
        "session_telemetry": [*history.telemetry, *(window.telemetry if window else ())],
        "login_events": [*history.logins, *(window.logins if window else ())],
        "crm_events": list(history.crm_events),
        "complaints": list(history.complaints),
        "scenario_instances": list(history.scenario_instances),
        "payment_truth": [*history.payment_truth, *(window.payment_truth if window else ())],
        "account_truth": list(dataset.account_truth),
        "schedule_truth": list(history.schedule_truth),
        "label_schedule": [*history.labels, *network.labels, *history.account_labels],
    }


def _by(*columns: str) -> Callable[[Row], Any]:
    return lambda row: tuple(row[c] for c in columns)


# The canonical order of each table's rows.
_ORDER: dict[str, Callable[[Row], Any]] = {
    "sim_registry_listings": _by("account_id"),
    "sim_phone_risk": _by("phone_number"),
    "account_truth": _by("account_id"),
    "schedule_truth": _by("schedule_id"),
    "ledger_entries": _by("at", "payment_id", "account_id"),
    "session_telemetry": _by("payment_id"),
    "payment_truth": _by("payment_id"),
    "label_schedule": _by("subject_type", "subject_id", "label_source"),
}


def canonical_tables(dataset: Dataset) -> dict[str, list[Row]]:
    return {
        table: _rows(items, _ORDER.get(table, _by("id")))
        for table, items in dataset_records(dataset).items()
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
