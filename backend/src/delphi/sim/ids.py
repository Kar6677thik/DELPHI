"""Deterministic ids for generated entities (docs/14 §2).

Every entity kind draws its ids from its own stream, ``keyed_rng(profile seed, "id", kind)``,
as ``uuid.UUID(bytes=rng.bytes(16), version=4)``. The same profile and seed give the same ids
on every run, and adding a new kind of entity never shifts the ids of the others. Entities
created live through the API use random UUIDv7s instead (``delphi.platform.ids``).
"""

from __future__ import annotations

import uuid

from delphi.sim.rng import keyed_rng

_UUID_BYTES = 16


class IdStream:
    """The ids of one entity kind, in generation order."""

    def __init__(self, seed: int, kind: str) -> None:
        self.kind = kind
        self._rng = keyed_rng(seed, "id", kind)

    def next(self) -> uuid.UUID:
        return uuid.UUID(bytes=self._rng.bytes(_UUID_BYTES), version=4)

    def take(self, n: int) -> list[uuid.UUID]:
        """The next ``n`` ids: the same as ``n`` calls to ``next()``, in one draw."""
        raw = self._rng.bytes(_UUID_BYTES * n)
        return [
            uuid.UUID(bytes=raw[i : i + _UUID_BYTES], version=4)
            for i in range(0, len(raw), _UUID_BYTES)
        ]
