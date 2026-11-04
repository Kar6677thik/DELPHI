"""RFC 9457 problem responses: the single place where errors become HTTP (docs/16 §1)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from delphi.api.request_id import current_request_id
from delphi.platform.errors import STATUS_BY_CODE, DelphiError, ProblemCode

PROBLEM_JSON = "application/problem+json"
_JSON = "application/json"

# Framework-raised HTTP errors (unknown route, wrong method) mapped to stable codes.
_CODE_BY_STATUS = {
    HTTPStatus.NOT_FOUND: ProblemCode.NOT_FOUND,
    HTTPStatus.METHOD_NOT_ALLOWED: ProblemCode.NOT_FOUND,
    HTTPStatus.UNAUTHORIZED: ProblemCode.UNAUTHENTICATED,
    HTTPStatus.FORBIDDEN: ProblemCode.FORBIDDEN,
}


class ProblemFieldError(BaseModel):
    path: str
    code: str
    message: str


class Problem(BaseModel):
    """The body of every error response: RFC 9457 plus the stable ``code`` (docs/16 §1)."""

    type: str
    title: str
    status: int
    detail: str
    code: ProblemCode
    instance: str
    request_id: str
    errors: list[ProblemFieldError] | None = None


# Declared on the app, so every operation documents its error body (docs/16 §1).
PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    "default": {"model": Problem, "description": "An RFC 9457 problem (docs/16 §1)"},
}


def document_problem_media_type(app: FastAPI) -> None:
    """FastAPI documents every response model as application/json. Error bodies are
    application/problem+json, so the generated contract says so (docs/16 preamble)."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = generate()
            for path_item in schema.get("paths", {}).values():
                for operation in path_item.values():
                    default = operation.get("responses", {}).get("default", {})
                    content = default.get("content", {})
                    if _JSON in content:
                        content[PROBLEM_JSON] = content.pop(_JSON)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def problem_response(
    code: ProblemCode,
    detail: str,
    *,
    instance: str,
    status: HTTPStatus | None = None,
    errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    http_status = status or STATUS_BY_CODE[code]
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": http_status.phrase,
        "status": int(http_status),
        "detail": detail,
        "code": code.value,
        "instance": instance,
        "request_id": current_request_id(),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(
        body, status_code=int(http_status), media_type=PROBLEM_JSON, headers=headers
    )


def problem(
    request: Request,
    code: ProblemCode,
    detail: str,
    *,
    status: HTTPStatus | None = None,
    errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return problem_response(
        code, detail, instance=request.url.path, status=status, errors=errors, headers=headers
    )


def internal_error_response(path: str) -> JSONResponse:
    """The response for an unhandled exception: no internals, only the request id."""
    return problem_response(ProblemCode.INTERNAL, "An internal error occurred.", instance=path)


async def _delphi_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DelphiError)
    return problem(request, exc.code, exc.detail)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {
            "path": ".".join(str(part) for part in err.get("loc", ())),
            "code": str(err.get("type", "invalid")),
            "message": str(err.get("msg", "")),
        }
        for err in exc.errors()
    ]
    return problem(request, ProblemCode.VALIDATION_FAILED, "The request is invalid.", errors=errors)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = HTTPStatus(exc.status_code)
    code = _CODE_BY_STATUS.get(status, ProblemCode.INTERNAL)
    return problem(
        request,
        code,
        status.description or status.phrase,
        status=status,
        headers=getattr(exc, "headers", None),
    )


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DelphiError, _delphi_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    # Unhandled exceptions are rendered by RequestContextMiddleware, which still knows the
    # request id; Starlette would render them outside every middleware.
