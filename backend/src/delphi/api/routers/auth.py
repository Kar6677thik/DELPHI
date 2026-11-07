"""Sign in, sign out, who am I (docs/16 §2 "Platform", docs/19 §2, FR-PLT-01)."""

from __future__ import annotations

import math
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from delphi.api.routers import API_PREFIX
from delphi.api.security import SESSION_COOKIE, Principal, current_principal
from delphi.platform.accounts import Permission, Role, authenticate, get_user
from delphi.platform.audit import ActorType, record
from delphi.platform.auth_sessions import LoginThrottle, end_session, start_session
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["auth"])

INVALID_LOGIN = "The username or password is incorrect."


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class MeView(BaseModel):
    id: UUID
    username: str
    display_name: str
    role: Role
    permissions: list[Permission]


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def client_ip(request: Request) -> str:
    """The caller's address. Behind Cloudflare the socket peer is cloudflared, so the
    configured header (`CF-Connecting-IP`) is used; only cloudflared can reach the api pod
    (docs/23 §4 NetworkPolicy), so the header cannot be forged from outside."""
    header = _platform(request).settings.client_ip_header
    if header and (value := request.headers.get(header)):
        return value.strip()
    return request.client.host if request.client else "unknown"


@router.post("/auth/login", status_code=204, response_class=Response)
def login(body: LoginRequest, request: Request, response: Response) -> Response:
    platform = _platform(request)
    throttle: LoginThrottle = request.app.state.login_throttle
    username, ip = body.username.strip().lower(), client_ip(request)
    wait = throttle.retry_after(username, ip)
    if wait is not None:
        raise DelphiError(
            "Too many failed sign-in attempts. Try again later.",
            code=ProblemCode.RATE_LIMITED,
            headers={"Retry-After": str(math.ceil(wait.total_seconds()))},
        )
    with platform.sessions.begin() as session:
        user = authenticate(session, username, body.password)
        if user is None:
            record(
                session,
                platform.clock,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="auth.login_failed",
                entity_type="user",
                entity_id=None,
                payload={"username": username, "ip": ip},
            )
            token = None
        else:
            token = start_session(
                session,
                platform.clock,
                platform.settings.session_policy,
                user,
                ip=ip,
                user_agent=request.headers.get("user-agent"),
                replaces=request.cookies.get(SESSION_COOKIE),
            )
            record(
                session,
                platform.clock,
                actor_type=ActorType.USER,
                actor_id=str(user.id),
                action="auth.login",
                entity_type="user",
                entity_id=str(user.id),
                payload={"ip": ip},
            )
    if token is None:
        throttle.failed(username, ip)
        raise DelphiError(INVALID_LOGIN, code=ProblemCode.UNAUTHENTICATED)
    throttle.succeeded(username, ip)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
        max_age=int(platform.settings.session_policy.absolute.total_seconds()),
    )
    response.status_code = 204
    return response


@router.post("/auth/logout", status_code=204, response_class=Response)
def logout(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(current_principal)],
) -> Response:
    platform = _platform(request)
    token = request.cookies.get(SESSION_COOKIE)
    with platform.sessions.begin() as session:
        if token:
            end_session(session, token)
        record(
            session,
            platform.clock,
            actor_type=ActorType.USER,
            actor_id=str(principal.user_id),
            action="auth.logout",
            entity_type="user",
            entity_id=str(principal.user_id),
        )
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    response.status_code = 204
    return response


@router.get("/auth/me")
def me(request: Request, principal: Annotated[Principal, Depends(current_principal)]) -> MeView:
    with _platform(request).sessions() as session:
        user = get_user(session, principal.user_id)
    if user is None:  # deleted between the two reads
        raise DelphiError("Sign in to continue.", code=ProblemCode.UNAUTHENTICATED)
    return MeView(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        permissions=sorted(user.permissions),
    )
