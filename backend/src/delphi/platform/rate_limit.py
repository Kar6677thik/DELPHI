"""Token buckets for the global API rate limit (docs/16 §1, docs/19 §1 "DoS", #74).

Each key (a session, or a client address without one) has a bucket of ``burst`` tokens that
refills at ``rate`` tokens per second; a request takes one token, and a request that finds the
bucket empty is refused with how long until the next token. Kept in process memory, like the
login throttle: the api runs one replica (docs/23 §4), and a restart refilling every bucket is
harmless. Buckets that are full again are dropped, so memory follows the active callers only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from delphi.platform.clock import Clock

# How often (in checks) full buckets are swept out of memory.
_SWEEP_EVERY = 1024


@dataclass(slots=True)
class _Bucket:
    tokens: float
    at: datetime


@dataclass(slots=True)
class TokenBuckets:
    clock: Clock
    rate: float  # tokens per second
    burst: int  # bucket size
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _checks: int = 0

    def __post_init__(self) -> None:
        if self.rate <= 0 or self.burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")

    def _refilled(self, bucket: _Bucket, now: datetime) -> float:
        elapsed = max((now - bucket.at).total_seconds(), 0.0)
        return min(float(self.burst), bucket.tokens + elapsed * self.rate)

    def take(self, key: str) -> float | None:
        """Take a token for ``key``: ``None`` if allowed, else the seconds until one is free."""
        now = self.clock.now()
        with self._lock:
            bucket = self._buckets.get(key)
            tokens = float(self.burst) if bucket is None else self._refilled(bucket, now)
            self._checks += 1
            if self._checks % _SWEEP_EVERY == 0:
                self._sweep(now)
            if tokens >= 1.0:
                self._buckets[key] = _Bucket(tokens - 1.0, now)
                return None
            self._buckets[key] = _Bucket(tokens, now)
            return (1.0 - tokens) / self.rate

    def _sweep(self, now: datetime) -> None:
        full = [k for k, b in self._buckets.items() if self._refilled(b, now) >= self.burst]
        for key in full:
            del self._buckets[key]

    def __len__(self) -> int:
        return len(self._buckets)
