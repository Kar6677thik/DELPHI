"""Deterministic ids for generated entities (docs/14 §2)."""

from __future__ import annotations

import uuid

from delphi.sim.ids import IdStream


def test_the_same_seed_and_kind_give_the_same_ids() -> None:
    assert IdStream(1, "customer").take(50) == IdStream(1, "customer").take(50)


def test_ids_are_version_4_rfc_4122_uuids() -> None:
    for value in IdStream(1, "payment").take(200):
        assert value.version == 4
        assert value.variant == uuid.RFC_4122


def test_take_is_the_same_as_repeated_next() -> None:
    stream = IdStream(20261031, "account")
    one_by_one = [stream.next() for _ in range(7)]
    assert IdStream(20261031, "account").take(7) == one_by_one
    # And a stream continues where a batch left off.
    batched = IdStream(20261031, "account")
    assert batched.take(3) + batched.take(4) == one_by_one


def test_kinds_and_seeds_have_independent_streams() -> None:
    customers = IdStream(1, "customer").take(100)
    assert set(customers).isdisjoint(IdStream(1, "account").take(100))
    assert set(customers).isdisjoint(IdStream(7, "customer").take(100))


def test_a_new_kind_never_shifts_existing_ids() -> None:
    before = IdStream(1, "payment").take(10)
    IdStream(1, "brand_new_kind").take(1000)
    assert IdStream(1, "payment").take(10) == before
