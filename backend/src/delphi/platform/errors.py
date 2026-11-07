"""Domain errors and their stable problem codes (docs/16 §1).

Modules raise ``DelphiError`` subclasses. The api layer maps them to RFC 9457
``application/problem+json`` in one place (``delphi.api.problems``). Codes are part of the API
contract: never rename one.
"""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus


class ProblemCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    CSRF_REJECTED = "CSRF_REJECTED"
    NOT_FOUND = "NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_CLIENT_SIGNALS = "INVALID_CLIENT_SIGNALS"
    PROBE_NOT_AWAITING = "PROBE_NOT_AWAITING"
    KB_VALIDATION_FAILED = "KB_VALIDATION_FAILED"
    FOUR_EYES_REQUIRED = "FOUR_EYES_REQUIRED"
    SHADOW_INSUFFICIENT = "SHADOW_INSUFFICIENT"
    KB_STALE_PARENT = "KB_STALE_PARENT"
    JOB_ALREADY_RUNNING = "JOB_ALREADY_RUNNING"
    NOT_INITIALISED = "NOT_INITIALISED"
    INTERNAL = "INTERNAL"


STATUS_BY_CODE: dict[ProblemCode, HTTPStatus] = {
    ProblemCode.VALIDATION_FAILED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ProblemCode.UNAUTHENTICATED: HTTPStatus.UNAUTHORIZED,
    ProblemCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
    ProblemCode.CSRF_REJECTED: HTTPStatus.FORBIDDEN,
    ProblemCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ProblemCode.IDEMPOTENCY_CONFLICT: HTTPStatus.CONFLICT,
    ProblemCode.INVALID_STATE_TRANSITION: HTTPStatus.CONFLICT,
    ProblemCode.VERSION_CONFLICT: HTTPStatus.PRECONDITION_FAILED,
    ProblemCode.RATE_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
    ProblemCode.INVALID_CLIENT_SIGNALS: HTTPStatus.BAD_REQUEST,
    ProblemCode.PROBE_NOT_AWAITING: HTTPStatus.CONFLICT,
    ProblemCode.KB_VALIDATION_FAILED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ProblemCode.FOUR_EYES_REQUIRED: HTTPStatus.CONFLICT,
    ProblemCode.SHADOW_INSUFFICIENT: HTTPStatus.CONFLICT,
    ProblemCode.KB_STALE_PARENT: HTTPStatus.CONFLICT,
    ProblemCode.JOB_ALREADY_RUNNING: HTTPStatus.CONFLICT,
    ProblemCode.NOT_INITIALISED: HTTPStatus.SERVICE_UNAVAILABLE,
    ProblemCode.INTERNAL: HTTPStatus.INTERNAL_SERVER_ERROR,
}


class DelphiError(Exception):
    """An expected failure with a stable code. ``detail`` is shown to the client, so it must
    never contain internals, secrets or personal data."""

    code: ProblemCode = ProblemCode.INTERNAL

    def __init__(
        self,
        detail: str,
        *,
        code: ProblemCode | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = headers  # e.g. Retry-After on RATE_LIMITED
        if code is not None:
            self.code = code

    @property
    def status(self) -> HTTPStatus:
        return STATUS_BY_CODE[self.code]


class NotFoundError(DelphiError):
    code = ProblemCode.NOT_FOUND


class InvalidStateTransitionError(DelphiError):
    code = ProblemCode.INVALID_STATE_TRANSITION


class VersionConflictError(DelphiError):
    code = ProblemCode.VERSION_CONFLICT
