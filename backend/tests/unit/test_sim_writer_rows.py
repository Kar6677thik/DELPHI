"""The COPY writer's row building (#58); the writes themselves are integration tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import Connection

from delphi.sim import bank, truth
from delphi.sim.writer import _copy, _rows, write_dataset


@dataclass(frozen=True)
class _One:
    payload: dict[str, Any]


def test_a_single_column_still_gives_tuples_and_jsonb_is_wrapped() -> None:
    (row,) = _rows(bank.sim_schedule, ["payload"], [_One({"amount_paise": 1})])
    assert isinstance(row, tuple) and isinstance(row[0], Jsonb)
    assert row[0].obj == {"amount_paise": 1}


def test_an_empty_table_copies_nothing() -> None:
    assert _copy(cast(Any, None), truth.schedule_truth, []) == 0


def test_the_writer_needs_psycopg_3() -> None:
    conn = SimpleNamespace(connection=SimpleNamespace(driver_connection=object()))
    with pytest.raises(TypeError, match="psycopg 3"):
        write_dataset(cast(Connection, conn), cast(Any, None))
