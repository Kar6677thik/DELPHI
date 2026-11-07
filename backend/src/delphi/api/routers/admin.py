"""User administration and audit verification (docs/16 §2 "Admin and jobs", FR-PLT-10).

Users are a mutable aggregate: responses carry `ETag: "<version>"` and PATCH needs a matching
`If-Match` (412 VERSION_CONFLICT otherwise, docs/16 §1). Disabling a user, changing their role or
resetting their password ends all their sessions, so the change takes effect at once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.platform.accounts import (
    MIN_PASSWORD_LENGTH,
    Role,
    UserChange,
    UserRecord,
    create_user,
    get_user_record,
    list_users,
    update_user,
)
from delphi.platform.audit import ActorType, verify
from delphi.platform.auth_sessions import end_user_sessions
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["admin"])
Admin = Annotated[Principal, Depends(require(Permission.ADMIN))]

USERNAME_PATTERN = r"^[A-Za-z0-9._-]{3,64}$"


class UserView(BaseModel):
    id: UUID
    username: str
    display_name: str
    role: Role
    disabled: bool
    created_at: datetime
    version: int

    @staticmethod
    def of(record: UserRecord) -> UserView:
        return UserView(
            id=record.id,
            username=record.username,
            display_name=record.display_name,
            role=record.role,
            disabled=record.disabled,
            created_at=record.created_at,
            version=record.version,
        )


class CreateUser(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    role: Role


class UpdateUser(BaseModel):
    """Only the fields present change."""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: Role | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=256)


class AuditVerifyView(BaseModel):
    ok: bool
    checked: int
    first_bad_id: int | None = None


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def _etag(version: int) -> str:
    return f'"{version}"'


def _if_match(raw: str | None) -> int:
    if raw is None:
        raise DelphiError(
            "This change needs an If-Match header with the user's ETag.",
            code=ProblemCode.VALIDATION_FAILED,
        )
    value = raw.strip().removeprefix("W/").strip('"')
    if not value.isdigit():
        raise DelphiError(
            "If-Match must be the ETag the server returned.", code=ProblemCode.VALIDATION_FAILED
        )
    return int(value)


@router.get("/admin/users")
def read_users(request: Request, _admin: Admin) -> list[UserView]:
    with _platform(request).sessions() as session:
        return [UserView.of(u) for u in list_users(session)]


@router.post("/admin/users", status_code=201)
def add_user(body: CreateUser, request: Request, response: Response, admin: Admin) -> UserView:
    platform = _platform(request)
    try:
        with platform.sessions.begin() as session:
            created = create_user(
                session,
                platform.clock,
                username=body.username,
                display_name=body.display_name,
                password=body.password,
                role=body.role,
                actor_type=ActorType.USER,
                actor_id=str(admin.user_id),
            )
            record = get_user_record(session, created.id)
    except IntegrityError:
        raise DelphiError("That username is taken.", code=ProblemCode.VALIDATION_FAILED) from None
    assert record is not None
    response.headers["ETag"] = _etag(record.version)
    return UserView.of(record)


@router.patch("/admin/users/{user_id}")
def change_user(
    user_id: UUID,
    body: UpdateUser,
    *,
    request: Request,
    response: Response,
    admin: Admin,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> UserView:
    platform = _platform(request)
    expected = _if_match(if_match)
    change = UserChange(
        display_name=body.display_name,
        role=body.role,
        disabled=body.disabled,
        password=body.password,
    )
    with platform.sessions.begin() as session:
        updated, ends_sessions = update_user(
            session,
            platform.clock,
            user_id,
            change,
            expected_version=expected,
            actor_id=admin.user_id,
        )
        if ends_sessions:
            end_user_sessions(session, user_id)
    response.headers["ETag"] = _etag(updated.version)
    return UserView.of(updated)


@router.get("/admin/audit/verify")
def verify_audit(
    request: Request,
    _admin: Admin,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
) -> AuditVerifyView:
    with _platform(request).sessions() as session:
        result = verify(session, since=since, until=until)
    return AuditVerifyView(ok=result.ok, checked=result.checked, first_bad_id=result.first_bad_id)
