"""KB versions in PostgreSQL (docs/07 §1, docs/17 §3, FR-KB-02).

A version is stored as canonical JSON (``delphi.kb.canonical``) with its SHA-256. Reading a
version re-validates the content into a ``KnowledgeBase`` and checks the hash, so a row that was
edited outside the app is caught instead of being used.

``ensure_kb_v1`` is what ``delphi migrate`` runs after the schema upgrade: it inserts the
repository's KB v1 as the ACTIVE version when the table is empty, and does nothing otherwise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Session

from delphi.kb.canonical import canonical_json, content_sha256
from delphi.kb.model import KnowledgeBase
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.health import CheckResult, CheckStatus
from delphi.platform.locks import KB_SEED_LOCK_KEY

metadata = MetaData(schema="delphi")

kb_versions = Table(
    "kb_versions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("status", Text, nullable=False),
    Column("parent_id", Integer),
    Column("content", JSONB, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("change_summary", Text, nullable=False),
    Column("created_by", PgUUID(as_uuid=True)),
    Column("approved_by", PgUUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("activated_at", DateTime(timezone=True)),
    Column("retired_at", DateTime(timezone=True)),
    Column("rollback_reason", Text),
    Column("version", Integer, nullable=False),
)

KB_V1_SUMMARY = "KB v1, loaded from the repository's kb/ by delphi migrate"


class KbStatus(StrEnum):
    """The KB version status machine (docs/07 §1)."""

    CANDIDATE = "CANDIDATE"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"


class KbIntegrityError(RuntimeError):
    """A stored version whose content no longer matches its hash."""


@dataclass(frozen=True, slots=True)
class StoredVersion:
    id: int
    status: KbStatus
    parent_id: int | None
    content_sha256: str
    change_summary: str
    created_by: UUID | None
    approved_by: UUID | None
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None
    rollback_reason: str | None
    kb: KnowledgeBase


@dataclass(frozen=True, slots=True)
class VersionSummary:
    """A version without its content, for listings."""

    id: int
    status: KbStatus
    parent_id: int | None
    content_sha256: str
    change_summary: str
    created_by: UUID | None
    created_at: datetime
    activated_at: datetime | None


def content_of(kb: KnowledgeBase) -> Any:
    """The version's canonical JSON as plain values (what `content` stores)."""
    # The canonical JSON, parsed back: jsonb keeps numbers exactly, so re-serialising the stored
    # content canonically reproduces the hash.
    return json.loads(canonical_json(kb))


def insert_version(
    session: Session,
    clock: Clock,
    kb: KnowledgeBase,
    *,
    status: KbStatus,
    change_summary: str,
    parent_id: int | None = None,
    created_by: UUID | None = None,
) -> int:
    """Insert a version in the caller's transaction, audited, and return its id."""
    now = clock.now()
    sha = content_sha256(kb)
    version_id: int = session.execute(
        insert(kb_versions)
        .values(
            status=status.value,
            parent_id=parent_id,
            content=content_of(kb),
            content_sha256=sha,
            change_summary=change_summary,
            created_by=created_by,
            created_at=now,
            activated_at=now if status is KbStatus.ACTIVE else None,
        )
        .returning(kb_versions.c.id)
    ).scalar_one()
    record(
        session,
        clock,
        actor_type=ActorType.USER if created_by is not None else ActorType.SYSTEM,
        actor_id=str(created_by) if created_by is not None else None,
        action="kb.version.created",
        entity_type="kb_version",
        entity_id=str(version_id),
        payload={"status": status.value, "content_sha256": sha, "parent_id": parent_id},
    )
    return version_id


def ensure_kb_v1(session: Session, clock: Clock, kb: KnowledgeBase) -> int | None:
    """Insert ``kb`` as the ACTIVE KB v1 if no version exists yet; return its id, or ``None``
    when the table already holds a version (so ``delphi migrate`` can run on every deploy)."""
    # Serialise concurrent migrate runs: the second waits, then sees the first's row.
    session.execute(select(func.pg_advisory_xact_lock(KB_SEED_LOCK_KEY)))
    if session.execute(select(kb_versions.c.id).limit(1)).first() is not None:
        return None
    return insert_version(session, clock, kb, status=KbStatus.ACTIVE, change_summary=KB_V1_SUMMARY)


def _stored(row: Any) -> StoredVersion:
    kb = KnowledgeBase.model_validate(row.content)
    if content_sha256(kb) != row.content_sha256:
        raise KbIntegrityError(f"KB version {row.id}: content does not match its SHA-256")
    return StoredVersion(
        id=row.id,
        status=KbStatus(row.status),
        parent_id=row.parent_id,
        content_sha256=row.content_sha256,
        change_summary=row.change_summary,
        created_by=row.created_by,
        approved_by=row.approved_by,
        created_at=row.created_at,
        activated_at=row.activated_at,
        retired_at=row.retired_at,
        rollback_reason=row.rollback_reason,
        kb=kb,
    )


def get_version(conn: Connection | Session, version_id: int) -> StoredVersion | None:
    row = conn.execute(select(kb_versions).where(kb_versions.c.id == version_id)).one_or_none()
    return _stored(row) if row is not None else None


def list_versions(
    conn: Connection | Session, *, limit: int, before_id: int | None = None
) -> list[VersionSummary]:
    """Newest first (keyset on ``id``), without the content."""
    k = kb_versions.c
    query = select(
        k.id,
        k.status,
        k.parent_id,
        k.content_sha256,
        k.change_summary,
        k.created_by,
        k.created_at,
        k.activated_at,
    ).order_by(k.id.desc())
    if before_id is not None:
        query = query.where(k.id < before_id)
    return [
        VersionSummary(
            id=r.id,
            status=KbStatus(r.status),
            parent_id=r.parent_id,
            content_sha256=r.content_sha256,
            change_summary=r.change_summary,
            created_by=r.created_by,
            created_at=r.created_at,
            activated_at=r.activated_at,
        )
        for r in conn.execute(query.limit(limit))
    ]


def active_version(conn: Connection | Session) -> StoredVersion | None:
    """The ACTIVE version, or ``None`` before ``delphi migrate`` has loaded KB v1."""
    row = conn.execute(
        select(kb_versions).where(kb_versions.c.status == KbStatus.ACTIVE.value)
    ).one_or_none()
    return _stored(row) if row is not None else None


def active_version_id(conn: Connection | Session) -> int | None:
    """The ACTIVE version's id, without loading its content (for pinning a session to it)."""
    found: int | None = conn.execute(
        select(kb_versions.c.id).where(kb_versions.c.status == KbStatus.ACTIVE.value)
    ).scalar_one_or_none()
    return found


def kb_readiness(conn: Connection) -> CheckResult:
    """The ``/ready`` check (docs/16 §2): an ACTIVE KB version exists."""
    found = conn.execute(
        select(kb_versions.c.id).where(kb_versions.c.status == KbStatus.ACTIVE.value)
    ).first()
    if found is None:
        return CheckResult(CheckStatus.FAILED, "no ACTIVE KB version (run delphi migrate)")
    return CheckResult(CheckStatus.OK)
