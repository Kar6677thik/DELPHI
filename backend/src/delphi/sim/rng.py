"""Random streams keyed by name (docs/14 §1 rule 1, §5.2; docs/22 common random numbers).

A stream derived from a seed and names, not from its position in a spawn order, never shifts
when other streams are added, removed or drawn from: the same payment gets the same draws
under every policy, and a new scenario leaves the frozen response models unchanged.
"""

from __future__ import annotations

import hashlib

import numpy as np

_KEY_WORDS = 4  # 128 bits of the name hash, as four 32-bit spawn-key words
_WORD_BYTES = 4
# Generated floats keep 6 decimals (docs/14 §3, canonical export): math.exp and friends can
# differ in the last bit between C libraries, and the dataset must be identical on every
# platform (FR-SIM-01).
FLOAT_DECIMALS = 6


def quantise(value: float) -> float:
    """``value`` to the generator's float precision, the same on every platform."""
    return round(value, FLOAT_DECIMALS)


def keyed_rng(seed: int, *names: str) -> np.random.Generator:
    """A PCG64 stream for ``seed`` and the ``/``-joined ``names``."""
    digest = hashlib.sha256("/".join(names).encode()).digest()
    spawn_key = tuple(
        int.from_bytes(digest[i : i + _WORD_BYTES], "big")
        for i in range(0, _KEY_WORDS * _WORD_BYTES, _WORD_BYTES)
    )
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence(seed, spawn_key=spawn_key)))
