"""The canonical export of a generated dataset and its SHA-256 (docs/14 §1, FR-SIM-01).

Every table's rows are sorted by primary key and serialised as canonical JSON (sorted keys, no
insignificant whitespace, UUIDs and UTC timestamps as strings). Database-assigned columns such
as `created_at` are not part of the generated data, so they are never exported. The same
profile, seed, config and code give the same bytes on every run and machine.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from delphi.sim.population import Population

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


def _value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value


def canonical_tables(population: Population) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, tuple[DataclassInstance, ...]] = {
        "customers": population.customers,
        "accounts": population.accounts,
        "devices": population.devices,
        "payees": population.payees,
    }
    return {
        name: sorted(
            ({k: _value(v) for k, v in asdict(row).items()} for row in rows),
            key=lambda row: str(row["id"]),
        )
        for name, rows in tables.items()
    }


def canonical_export(population: Population) -> str:
    document = {
        "profile": population.profile,
        "history_start": _value(population.history_start),
        "history_end": _value(population.history_end),
        "tables": canonical_tables(population),
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def dataset_sha256(population: Population) -> str:
    return hashlib.sha256(canonical_export(population).encode("utf-8")).hexdigest()
