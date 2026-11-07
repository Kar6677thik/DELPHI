"""Idempotency keys for state-creating requests (docs/16 §1, FR-PLT-05).

A client sends `Idempotency-Key: <uuid>` with a request that creates state. The first request
with a key runs and its response is stored in the **same transaction** as its effects. A later
request with the same key and user:
- with the same endpoint and body: gets the stored response, and nothing runs again;
- with a different endpoint or body: is rejected with `409 IDEMPOTENCY_CONFLICT`.

Two concurrent requests with the same key are serialised by a transaction-scoped advisory lock
on (user, key), so the second waits for the first to commit and then replays it. Keys are kept
for ``retention`` (24 h); an older key counts as unused, and RETENTION_SWEEP deletes it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.tables import idempotency_keys


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: Any


def request_fingerprint(endpoint: str, body: bytes) -> bytes:
    """SHA-256 of the endpoint and the body. A JSON body is canonicalised first (sorted keys,
    no whitespace), so a client that re-serialises the same object still matches."""
    try:
        canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()
    except ValueError:
        canonical = body
    return hashlib.sha256(endpoint.encode() + b"\n" + canonical).digest()


def lookup(
    session: Session,
    *,
    user_id: UUID,
    key: UUID,
    endpoint: str,
    fingerprint: bytes,
    now: datetime,
    retention: timedelta,
) -> StoredResponse | None:
    """Lock (user, key) for this transaction and return the stored response, if any. Raises
    IDEMPOTENCY_CONFLICT when the key was used for a different request."""
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"{user_id}:{key}", 0)))
    )
    where = (idempotency_keys.c.key == key, idempotency_keys.c.user_id == user_id)
    row = session.execute(select(idempotency_keys).where(*where)).one_or_none()
    if row is None:
        return None
    if row.created_at < now - retention:
        session.execute(delete(idempotency_keys).where(*where))  # expired: the key is free
        return None
    if row.endpoint != endpoint or row.request_sha256 != fingerprint:
        raise DelphiError(
            "This Idempotency-Key was already used for a different request.",
            code=ProblemCode.IDEMPOTENCY_CONFLICT,
        )
    return StoredResponse(row.response_status, row.response_body)


def store(
    session: Session,
    *,
    user_id: UUID,
    key: UUID,
    endpoint: str,
    fingerprint: bytes,
    status: int,
    body: Any,
    now: datetime,
) -> None:
    """Record the response, in the transaction that made the request's changes."""
    session.execute(
        idempotency_keys.insert().values(
            key=key,
            user_id=user_id,
            endpoint=endpoint,
            request_sha256=fingerprint,
            response_status=status,
            response_body=body,
            created_at=now,
        )
    )
