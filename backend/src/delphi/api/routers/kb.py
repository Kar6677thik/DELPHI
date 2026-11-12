"""KB versions, read-only (docs/16 §2 "Scout, typologies, KB", FR-KB-04, #15).

List, get, diff and export. Creating, shadowing, promoting, rejecting and rolling back versions
arrive with the KB pages (#119).
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.kb.diff import DiffOp, diff_content
from delphi.kb.export import export_zip
from delphi.kb.store import (
    KbStatus,
    StoredVersion,
    VersionSummary,
    content_of,
    get_version,
    list_versions,
)
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["kb"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200  # docs/16 §1

Viewer = Annotated[Principal, Depends(require(Permission.VIEW))]


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


class KbVersionSummaryView(BaseModel):
    id: int
    status: KbStatus
    parent_id: int | None
    content_sha256: str
    change_summary: str
    created_by: UUID | None
    created_at: datetime
    activated_at: datetime | None

    @staticmethod
    def of(v: VersionSummary | StoredVersion) -> KbVersionSummaryView:
        return KbVersionSummaryView(
            id=v.id,
            status=v.status,
            parent_id=v.parent_id,
            content_sha256=v.content_sha256,
            change_summary=v.change_summary,
            created_by=v.created_by,
            created_at=v.created_at,
            activated_at=v.activated_at,
        )


class KbVersionView(KbVersionSummaryView):
    approved_by: UUID | None
    retired_at: datetime | None
    rollback_reason: str | None
    content: dict[str, Any]


class KbVersionPage(BaseModel):
    items: list[KbVersionSummaryView]
    next_cursor: str | None


class KbChangeView(BaseModel):
    path: str  # JSON Pointer (RFC 6901) into the content
    op: DiffOp
    before: Any = None
    after: Any = None


class KbDiffView(BaseModel):
    version_id: int
    against_id: int
    changes: list[KbChangeView]


def _encode_cursor(version_id: int) -> str:
    return base64.urlsafe_b64encode(str(version_id).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise DelphiError("The cursor is not valid.", code=ProblemCode.VALIDATION_FAILED) from None


def _get(request: Request, version_id: int) -> StoredVersion:
    with _platform(request).sessions() as session:
        version = get_version(session, version_id)
    if version is None:
        raise NotFoundError("No KB version with this id.")
    return version


@router.get("/kb/versions")
def read_kb_versions(
    request: Request,
    _principal: Viewer,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> KbVersionPage:
    """Newest first, without the content."""
    before = _decode_cursor(cursor) if cursor else None
    with _platform(request).sessions() as session:
        found = list_versions(session, limit=limit + 1, before_id=before)
    page, more = found[:limit], len(found) > limit
    return KbVersionPage(
        items=[KbVersionSummaryView.of(v) for v in page],
        next_cursor=_encode_cursor(page[-1].id) if more and page else None,
    )


@router.get("/kb/versions/{version_id}")
def read_kb_version(version_id: int, request: Request, _principal: Viewer) -> KbVersionView:
    v = _get(request, version_id)
    return KbVersionView(
        **KbVersionSummaryView.of(v).model_dump(),
        approved_by=v.approved_by,
        retired_at=v.retired_at,
        rollback_reason=v.rollback_reason,
        content=content_of(v.kb),
    )


@router.get("/kb/versions/{version_id}/diff")
def read_kb_diff(
    version_id: int,
    request: Request,
    _principal: Viewer,
    against: Annotated[int | None, Query(description="default: the version's parent")] = None,
) -> KbDiffView:
    """What changed from ``against`` (default: the parent) to this version."""
    version = _get(request, version_id)
    against_id = against if against is not None else version.parent_id
    if against_id is None:
        raise DelphiError(
            "This version has no parent: pass `against`.", code=ProblemCode.VALIDATION_FAILED
        )
    base = _get(request, against_id)
    changes = diff_content(content_of(base.kb), content_of(version.kb))
    return KbDiffView(
        version_id=version.id,
        against_id=base.id,
        changes=[
            KbChangeView(path=c.path, op=c.op, before=c.before, after=c.after) for c in changes
        ],
    )


@router.get(
    "/kb/versions/{version_id}/export",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}, "description": "The YAML tree"}},
)
def export_kb_version(version_id: int, request: Request, _principal: Viewer) -> Response:
    """The version as the `kb/` YAML tree, zipped (docs/07 §1)."""
    v = _get(request, version_id)
    return Response(
        export_zip(v.kb, version_id=v.id, content_sha256=v.content_sha256),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="delphi-kb-v{v.id}.zip"'},
    )
