"""Users, roles, permissions and passwords (docs/19 §2–3).

The permission matrix lives here, in one place: routers ask for a permission, never a role.
Passwords are hashed with Argon2id (argon2-cffi defaults: time cost 3, 64 MiB, parallelism 4)
and need at least 12 characters.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.ids import uuid7
from delphi.platform.logging import get_logger
from delphi.platform.tables import users

log = get_logger(__name__)

MIN_PASSWORD_LENGTH = 12  # docs/19 §2
BOOTSTRAP_LOCK_KEY = 44  # pg_advisory_xact_lock key for the bootstrap admin (42 audit, 43 outbox)

_hasher = PasswordHasher()  # argon2-cffi defaults are the docs/19 §2 parameters
# Verifying against this when the username is unknown costs the same as a real check, so the
# response time does not reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-password")


class Role(StrEnum):
    VIEWER = "VIEWER"
    INVESTIGATOR = "INVESTIGATOR"
    FDE_ENGINEER = "FDE_ENGINEER"
    ADMIN = "ADMIN"


class Permission(StrEnum):
    """docs/19 §3."""

    VIEW = "view"
    DEMO_DRIVE = "demo:drive"
    CASE_WRITE = "case:write"
    CHASE_WRITE = "chase:write"
    FDE = "fde"
    ADMIN = "admin"


_INVESTIGATOR = frozenset(
    {Permission.VIEW, Permission.DEMO_DRIVE, Permission.CASE_WRITE, Permission.CHASE_WRITE}
)
PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.VIEW}),
    Role.INVESTIGATOR: _INVESTIGATOR,
    Role.FDE_ENGINEER: _INVESTIGATOR | {Permission.FDE},
    Role.ADMIN: frozenset(Permission),
}


@dataclass(frozen=True, slots=True)
class User:
    id: UUID
    username: str
    display_name: str
    role: Role
    disabled: bool

    @property
    def permissions(self) -> frozenset[Permission]:
        return frozenset() if self.disabled else PERMISSIONS[self.role]


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise DelphiError(
            f"A password needs at least {MIN_PASSWORD_LENGTH} characters.",
            code=ProblemCode.VALIDATION_FAILED,
        )
    return _hasher.hash(password)


def _user(row: object) -> User:
    return User(row.id, row.username, row.display_name, Role(row.role), row.disabled)  # type: ignore[attr-defined]


def get_user(session: Session, user_id: UUID) -> User | None:
    row = session.execute(select(users).where(users.c.id == user_id)).one_or_none()
    return _user(row) if row is not None else None


def authenticate(session: Session, username: str, password: str) -> User | None:
    """The user if the credentials are right and the account is enabled, else None. The
    caller answers every failure with the same generic message (no user enumeration)."""
    row = session.execute(
        select(users).where(users.c.username == username.strip().lower())
    ).one_or_none()
    try:
        _hasher.verify(row.password_hash if row is not None else _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return None
    if row is None or row.disabled:
        return None
    return _user(row)


def create_user(
    session: Session,
    clock: Clock,
    *,
    username: str,
    display_name: str,
    password: str,
    role: Role,
    actor_type: ActorType,
    actor_id: str | None,
) -> User:
    """Create a user and audit it in the caller's transaction. Usernames are stored lower-case."""
    user_id = uuid7()
    name = username.strip().lower()
    session.execute(
        users.insert().values(
            id=user_id,
            username=name,
            display_name=display_name,
            password_hash=hash_password(password),
            role=role.value,
            disabled=False,
            created_at=clock.now(),
            version=1,
        )
    )
    record(
        session,
        clock,
        actor_type=actor_type,
        actor_id=actor_id,
        action="user.created",
        entity_type="user",
        entity_id=str(user_id),
        payload={"username": name, "role": role.value},
    )
    return User(user_id, name, display_name, role, disabled=False)


class BootstrapError(Exception):
    """The users table is empty and no bootstrap admin is configured (FR-PLT-03)."""


def ensure_bootstrap_admin(
    session: Session, clock: Clock, *, username: str | None, password: str | None
) -> User | None:
    """On start: if there are no users, create the ADMIN from the environment, or fail fast
    when it is not configured (docs/19 §2). Returns the new admin, or None if users exist."""
    session.execute(select(func.pg_advisory_xact_lock(BOOTSTRAP_LOCK_KEY)))
    if session.execute(select(func.count()).select_from(users)).scalar_one() > 0:
        return None
    if not username or not password:
        raise BootstrapError(
            "no users exist: set DELPHI_BOOTSTRAP_ADMIN_USERNAME and "
            "DELPHI_BOOTSTRAP_ADMIN_PASSWORD to create the first ADMIN"
        )
    admin = create_user(
        session,
        clock,
        username=username,
        display_name="Administrator",
        password=password,
        role=Role.ADMIN,
        actor_type=ActorType.SYSTEM,
        actor_id=None,
    )
    log.warning(
        "auth.bootstrap_admin_created",
        username=admin.username,
        hint="sign in and rotate this password",
    )
    return admin
