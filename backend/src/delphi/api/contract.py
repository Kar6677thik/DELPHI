"""The committed API contract, ``backend/openapi.json`` (docs/16 preamble, docs/21 §1).

The frontend generates its types from this file, and CI fails when it is out of date.
"""

from __future__ import annotations

import json
from pathlib import Path

from delphi.api.app import create_app
from delphi.platform.settings import Settings
from delphi.platform.wiring import build_platform

# src/delphi/api/contract.py → backend/
OPENAPI_FILE = Path(__file__).resolve().parents[3] / "openapi.json"
# The document must not change with the build's git SHA, so it is always generated as "dev".
CONTRACT_VERSION = "dev"
# Generating the document never opens a connection (the engine is lazy); the URL only has to
# be a valid PostgreSQL URL. Nothing listens on port 1.
_UNUSED_DATABASE_URL = "postgresql://contract:unused@127.0.0.1:1/none"


def openapi_document() -> str:
    """The OpenAPI document as stable, diff-friendly JSON text."""
    settings = Settings(database_url=_UNUSED_DATABASE_URL, version=CONTRACT_VERSION)
    platform = build_platform(settings, process="contract")
    try:
        schema = create_app(platform).openapi()
    finally:
        platform.close()
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
