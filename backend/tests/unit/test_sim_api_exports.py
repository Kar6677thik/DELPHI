"""`delphi.sim.api` re-exports the `bank` tables for feature modules, and nothing from
`sim_truth` (docs/04 §4.1 rules 3 and 5)."""

from __future__ import annotations

from sqlalchemy import Table

from delphi.sim import api, bank, truth


def _exported_tables() -> dict[str, Table]:
    return {name: obj for name, obj in vars(api).items() if isinstance(obj, Table)}


def test_no_ground_truth_table_is_exported() -> None:
    exported = _exported_tables().values()
    assert not [t.fullname for t in exported if t.schema == "sim_truth"]
    assert not set(exported) & set(truth.metadata.tables.values())


def test_every_bank_table_but_the_sim_schedule_is_exported() -> None:
    exported = {t.fullname for t in _exported_tables().values() if t.schema == "bank"}
    expected = {t.fullname for t in bank.metadata.tables.values()} - {"bank.sim_schedule"}
    assert exported == expected


def test_exports_are_the_simulators_own_definitions() -> None:
    for table in _exported_tables().values():
        if table.schema == "bank":
            assert bank.metadata.tables[table.fullname] is table
