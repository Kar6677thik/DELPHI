"""Advisory lock keys (docs/17 §4, #78): one place, no two features on one key."""

from __future__ import annotations

from delphi.platform import locks


def test_every_lock_key_is_distinct() -> None:
    keys = [v for k, v in vars(locks).items() if k.endswith("_LOCK_KEY")]
    assert sorted(keys) == sorted(locks.ALL_LOCK_KEYS.values())
    assert len(set(keys)) == len(keys)
