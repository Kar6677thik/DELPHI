"""Serves the built single-page app at ``/`` (docs/18, ADR-0008).

The SPA is a fallback for requests that match no route, not a route of its own: a 404
handler serves ``index.html`` (or a static file) for a GET or HEAD on a non-``/api`` path and
leaves everything else to the problem handlers. So routing is the same with or without a
build: an unknown ``/api/*`` path is a 404 problem+json for every method (FR-DEP-04), a wrong
method on an API route stays a 405, and trailing-slash redirects still happen.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from starlette.responses import Response

from delphi.api.problems import http_error
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
_SPA_METHODS = frozenset({"GET", "HEAD"})

log = get_logger(__name__)


def _is_api_path(path: str) -> bool:
    return path == _API_ROOT or path.startswith(f"{_API_ROOT}/")


def _has_dot_segment(path: str) -> bool:
    """Dotfiles (``.env``, ``.git``) and ``..`` are never served."""
    return any(part.startswith(".") for part in path.split("/"))


def _static_file(root: Path, path: str) -> Path | None:
    """The file under ``root`` that ``path`` names, or None. Never escapes ``root``."""
    if not path:
        return None
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def install_spa(app: FastAPI, static_dir: Path) -> bool:
    """Serve the SPA for requests that match no route.

    Returns False, and serves nothing, when ``static_dir`` holds no ``index.html`` (a
    backend-only checkout or CI job).
    """
    root = static_dir.resolve()
    index = root / INDEX_FILE
    if not index.is_file():
        log.warning("spa.static_missing", static_dir=str(static_dir))
        return False
    assets = root / ASSETS_DIR

    async def spa_or_problem(request: Request, exc: Exception) -> Response:
        path = request.url.path.lstrip("/")
        servable = (
            request.method in _SPA_METHODS and not _is_api_path(path) and not _has_dot_segment(path)
        )
        found = _static_file(root, path) if servable else None
        # Any other path is a client-side route, except a missing hashed asset: that is a
        # stale page, and gets a 404.
        if found is None and servable and not path.startswith(f"{ASSETS_DIR}/"):
            found = index
        if found is None:
            return await http_error(request, exc)
        cache = IMMUTABLE_CACHE if found.is_relative_to(assets) else REVALIDATE_CACHE
        return FileResponse(found, headers={"Cache-Control": cache})

    app.add_exception_handler(_NOT_FOUND, spa_or_problem)
    return True
