"""Who is calling, and may they? (docs/16 §1, docs/19 §2–3)

Every protected router declares ``Depends(require(Permission.X))``. ``current_principal``
resolves the `delphi_session` cookie to an enabled user; anything else is 401. A user without
the permission gets 403. A route that declares nothing is public, so the RBAC test
(tests/integration/test_rbac.py) fails when a route is not listed with its permission: deny by
default is enforced by that test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Request

from delphi.platform.accounts import Permission, User
from delphi.platform.auth_sessions import resolve_session
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.wiring import Platform

__all__ = ["SESSION_COOKIE", "Permission", "Principal", "current_principal", "require"]

SESSION_COOKIE = "delphi_session"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    permissions: frozenset[Permission]

    @staticmethod
    def of(user: User) -> Principal:
        return Principal(user.id, user.permissions)


def current_principal(request: Request) -> Principal:
    """The signed-in caller, from the session cookie; 401 if there is none or it has ended."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        platform: Platform = request.app.state.platform
        with platform.sessions.begin() as session:
            user = resolve_session(session, platform.clock, platform.settings.session_policy, token)
        if user is not None:
            return Principal.of(user)
    raise DelphiError("Sign in to continue.", code=ProblemCode.UNAUTHENTICATED)


def require(permission: Permission) -> Callable[[Principal], Principal]:
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:  # noqa: B008
        if permission not in principal.permissions:
            raise DelphiError("You do not have permission to do this.", code=ProblemCode.FORBIDDEN)
        return principal

    return dependency
