"""The Scout's facade for the api (docs/04 §4.1 rule 3)."""

from __future__ import annotations

from delphi.scout.drafts import (
    DraftInvalidError,
    DraftPatch,
    DraftSpec,
    DraftStatus,
    backtest_target,
    candidate_kb,
    edit_draft,
    get_draft,
    list_drafts,
    stored_spec,
    validate_draft,
)
from delphi.scout.queue import (
    QueueCursor,
    QueueItem,
    SubjectType,
    count_queue_items,
    dismiss,
    queue_item,
    queue_items,
)
from delphi.scout.run import run_scout
from delphi.scout.tables import scout_clusters, typology_drafts

__all__ = [
    "DraftInvalidError",
    "DraftPatch",
    "DraftSpec",
    "DraftStatus",
    "QueueCursor",
    "QueueItem",
    "SubjectType",
    "backtest_target",
    "candidate_kb",
    "count_queue_items",
    "dismiss",
    "edit_draft",
    "get_draft",
    "list_drafts",
    "queue_item",
    "queue_items",
    "run_scout",
    "scout_clusters",
    "stored_spec",
    "typology_drafts",
    "validate_draft",
]
