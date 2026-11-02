"""Deterministic per-session RNG for exploration (docs/06 §10)."""

from __future__ import annotations

import hashlib

import numpy as np


def session_seed(payment_id: str, exploration_seed: int) -> int:
    """sha256(f"{payment_id}:{exploration_seed}")[:8] as a non-negative int63."""
    digest = hashlib.sha256(f"{payment_id}:{exploration_seed}".encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 1


def session_rng(payment_id: str, exploration_seed: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(session_seed(payment_id, exploration_seed)))
