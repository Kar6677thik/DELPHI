"""UUIDv7 identifiers (RFC 9562 §5.7; docs/16 §1, docs/17 §1).

Python 3.12 has no ``uuid.uuid7``. A v7 id is a 48-bit Unix-millisecond timestamp, the
version nibble, 12 random bits, the variant bits and 62 random bits, so ids sort by creation
time, which keeps B-tree inserts local. Ids made in the same millisecond are not ordered among
themselves; nothing in DELPHI depends on that.

Simulated entities do not use this: the generator derives deterministic ids (docs/14 §1).
"""

from __future__ import annotations

import secrets
import time
import uuid

_MAX_MS = (1 << 48) - 1
_VERSION = 0x7
_VARIANT = 0b10


def uuid7(unix_ms: int | None = None) -> uuid.UUID:
    ms = time.time_ns() // 1_000_000 if unix_ms is None else unix_ms
    if not 0 <= ms <= _MAX_MS:
        raise ValueError(f"timestamp {ms} ms does not fit in 48 bits")
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ms << 80) | (_VERSION << 76) | (rand_a << 64) | (_VARIANT << 62) | rand_b
    return uuid.UUID(int=value)


def uuid7_unix_ms(value: uuid.UUID) -> int:
    """The creation time embedded in a v7 id."""
    return value.int >> 80
