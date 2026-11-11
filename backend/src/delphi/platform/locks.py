"""Every `pg_advisory_xact_lock` key DELPHI uses, in one place (docs/17 §4, #78).

A transaction-scoped advisory lock serialises one kind of write across processes. Two features
sharing a key would only wait on each other needlessly, but keys must stay unique: a test checks
it. Feature modules import their key from here (``delphi.platform`` holds no business logic, only
the numbers). The idempotency lock is not listed: it hashes the (user, key) pair instead.
"""

from __future__ import annotations

AUDIT_LOCK_KEY = 42  # the audit hash chain (docs/17 §4)
OUTBOX_LOCK_KEY = 43  # outbox publishing, so events commit in id order (docs/04 §5.5)
BOOTSTRAP_LOCK_KEY = 44  # creating the first ADMIN (docs/19 §2)
DATASET_REQUEST_LOCK_KEY = 45  # queuing GENERATE_DATASET (POST /admin/datasets, delphi seed)
DATASET_WRITE_LOCK_KEY = 46  # writing a generated dataset into the bank
KB_SEED_LOCK_KEY = 47  # delphi migrate inserting KB v1 (docs/07 §1)

ALL_LOCK_KEYS: dict[str, int] = {
    "audit": AUDIT_LOCK_KEY,
    "outbox": OUTBOX_LOCK_KEY,
    "bootstrap": BOOTSTRAP_LOCK_KEY,
    "dataset_request": DATASET_REQUEST_LOCK_KEY,
    "dataset_write": DATASET_WRITE_LOCK_KEY,
    "kb_seed": KB_SEED_LOCK_KEY,
}
