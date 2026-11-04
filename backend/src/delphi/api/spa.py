"""Serves the built single-page app at ``/`` (docs/18, ADR-0008).

The SPA fallback answers only non-``/api`` paths. An unknown ``/api/*`` path still raises a
404, which the problem handlers render as problem+json (FR-DEP-04).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from delphi.api.routers import API_PREFIX
from delphi.platform.logging import get_logger

INDEX_FILE = "index.html"
ASSETS_DIR = "assets"
# Vite content-hashes every file under assets/, so a name never changes content.
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
# index.html and the other unhashed files are revalidated, so a deploy shows at once.
REVALIDATE_CACHE = "no-cache"
_API_ROOT = API_PREFIX.strip("/").split("/")[0]
_NOT_FOUND = 404

log = get_logger(__name__)


def _is_api_path(path: str) -> bool:
    return path == _API_ROOT or path.startswith(f"{_API_ROOT}/")


def _static_file(root: Path, path: str) -> Path | None:
    """The file under ``root`` that ``path`` names, or None. Never escapes ``root``."""
    if not path:
        return None
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def install_spa(app: FastAPI, static_dir: Path) -> bool:
    """Add the SPA catch-all route. Call it after every API router is included.

    Returns False, and serves nothing, when ``static_dir`` holds no ``index.html`` (a
    backend-only checkout or CI job).
    """
    root = static_dir.resolve()
    index = root / INDEX_FILE
    if not index.is_file():
        log.warning("spa.static_missing", static_dir=str(static_dir))
        return False
    assets = root / ASSETS_DIR

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if _is_api_path(path):
            raise StarletteHTTPException(status_code=_NOT_FOUND)
        found = _static_file(root, path)
        if found is not None:
            cache = IMMUTABLE_CACHE if found.is_relative_to(assets) else REVALIDATE_CACHE
            return FileResponse(found, headers={"Cache-Control": cache})
        if path.startswith(f"{ASSETS_DIR}/"):
            # A missing hashed asset is a stale page, not a client-side route.
            raise StarletteHTTPException(status_code=_NOT_FOUND)
        return FileResponse(index, headers={"Cache-Control": REVALIDATE_CACHE})

    return True
