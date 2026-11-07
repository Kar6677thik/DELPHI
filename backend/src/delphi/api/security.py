"""Who is calling, and may they? (docs/16 §1, docs/19 §3)

Every protected router declares ``Depends(require("<permission>"))``. Until authentication
lands (#6), ``current_principal`` rejects every request with 401, so any endpoint that declares
a permission is closed rather than accidentally public. #6 replaces only the body of
``current_principal`` (session cookie → user → role permissions); routers do not change. Tests
override ``current_principal`` through ``app.dependency_overrides``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from fastapi import Depends, Request

from delphi.platform.errors import DelphiError, ProblemCode


class Permission(StrEnum):
    """docs/19 §3."""

    VIEW = "view"
    DEMO_DRIVE = "demo:drive"
    CASE_WRITE = "case:write"
    CHASE_WRITE = "chase:write"
    FDE = "fde"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    permissions: frozenset[Permission]


def current_principal(request: Request) -> Principal:
    """The authenticated caller. Authentication is #6; until then nobody is authenticated."""
    raise DelphiError("Sign in to continue.", code=ProblemCode.UNAUTHENTICATED)


def require(permission: Permission) -> Callable[[Principal], Principal]:
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:  # noqa: B008
        if permission not in principal.permissions:
            raise DelphiError("You do not have permission to do this.", code=ProblemCode.FORBIDDEN)
        return principal

    return dependency
