"""The hash-chained audit log (docs/17 §4, FR-PLT-04).

``record`` appends an entry in the **caller's transaction**, so the entry exists exactly when the
change it describes does: a rollback removes both. Each entry's hash covers its content and the
previous entry's hash::

    hash = SHA-256(prev_hash || canonical_json({at, actor_type, actor_id, action, entity_type,
                                                entity_id, payload}))

and the first entry's ``prev_hash`` is 32 zero bytes. Writers take ``pg_advisory_xact_lock(42)``
so the chain is serial: concurrent writers wait for each other rather than forking the chain.
The application roles can only INSERT and SELECT `audit_log` (docs/17 §2), so rewriting history
needs the schema owner; ``verify`` detects that it happened.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import Clock
from delphi.platform.locks import AUDIT_LOCK_KEY
from delphi.platform.tables import audit_log

GENESIS = bytes(32)


class ActorType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    ENGINE = "ENGINE"
    LLM = "LLM"
    JOB = "JOB"


def entry_hash(
    prev_hash: bytes,
    *,
    at: datetime,
    actor_type: str,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str | None,
    payload: Mapping[str, Any],
) -> bytes:
    content = {
        "at": at.astimezone(UTC).isoformat(),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "payload": payload,
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(prev_hash + canonical.encode()).digest()


def record(
    session: Session,
    clock: Clock,
    *,
    actor_type: ActorType,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str | None,
    payload: Mapping[str, Any] | None = None,
) -> int:
    """Append an audit entry in the caller's transaction and return its id. ``payload`` must be
    JSON-serialisable and must not contain secrets (it is kept forever)."""
    session.execute(select(func.pg_advisory_xact_lock(AUDIT_LOCK_KEY)))
    prev = session.execute(
        select(audit_log.c.hash).order_by(audit_log.c.id.desc()).limit(1)
    ).scalar_one_or_none()
    prev_hash = prev if prev is not None else GENESIS
    # Microseconds survive the round trip through timestamptz, so verify recomputes the same.
    at = clock.now()
    content = dict(payload or {})
    digest = entry_hash(
        prev_hash,
        at=at,
        actor_type=actor_type.value,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=content,
    )
    entry_id: int = session.execute(
        audit_log.insert()
        .values(
            at=at,
            actor_type=actor_type.value,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=content,
            prev_hash=prev_hash,
            hash=digest,
        )
        .returning(audit_log.c.id)
    ).scalar_one()
    return entry_id


@dataclass(frozen=True, slots=True)
class Verification:
    ok: bool
    checked: int
    first_bad_id: int | None = None


def verify(
    session: Session, *, since: datetime | None = None, until: datetime | None = None
) -> Verification:
    """Recompute the chain for entries with ``since <= at < until`` (all when omitted). The first
    entry checked must link to the entry before it (or to the genesis hash), so deleting or
    editing an earlier entry is caught too."""
    query = select(audit_log).order_by(audit_log.c.id)
    if since is not None:
        query = query.where(audit_log.c.at >= since)
    if until is not None:
        query = query.where(audit_log.c.at < until)
    checked = 0
    expected_prev: bytes | None = None
    for row in session.execute(query):
        if expected_prev is None:
            before = session.execute(
                select(audit_log.c.hash)
                .where(audit_log.c.id < row.id)
                .order_by(audit_log.c.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            expected_prev = before if before is not None else GENESIS
        recomputed = entry_hash(
            row.prev_hash,
            at=row.at,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            payload=row.payload,
        )
        if row.prev_hash != expected_prev or recomputed != row.hash:
            return Verification(ok=False, checked=checked, first_bad_id=row.id)
        checked += 1
        expected_prev = row.hash
    return Verification(ok=True, checked=checked)


class AuditChainBroken(Exception):
    """Raised by the AUDIT_VERIFY job, so the failure shows up as a FAILED job (docs/20 §4)."""


@dataclass(frozen=True, slots=True)
class AuditVerifyJob:
    """The daily AUDIT_VERIFY job (docs/04 §5.4): verify the last ``window`` of the chain."""

    sessions: sessionmaker[Session]
    clock: Clock
    window: timedelta = timedelta(hours=24)

    def __call__(self, _ctx: object) -> Mapping[str, Any]:
        with self.sessions() as session:
            result = verify(session, since=self.clock.now() - self.window)
        if not result.ok:
            raise AuditChainBroken(f"audit chain broken at entry {result.first_bad_id}")
        return {"ok": True, "checked": result.checked}
