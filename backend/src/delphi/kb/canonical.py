"""Canonical JSON and content hashing of a KB version (docs/07 §1)."""

from __future__ import annotations

import hashlib
import json

from delphi.kb.model import KnowledgeBase


def canonical_json(kb: KnowledgeBase) -> str:
    """UTF-8 JSON, keys sorted, no insignificant whitespace, floats in repr round-trip form."""
    return json.dumps(
        kb.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_sha256(kb: KnowledgeBase) -> str:
    return hashlib.sha256(canonical_json(kb).encode("utf-8")).hexdigest()
