"""The Scout's facade for the api (docs/04 §4.1 rule 3)."""

from __future__ import annotations

from delphi.scout.queue import (
    QueueCursor,
    QueueItem,
    SubjectType,
    count_queue_items,
    dismiss,
    queue_item,
    queue_items,
)

__all__ = [
    "QueueCursor",
    "QueueItem",
    "SubjectType",
    "count_queue_items",
    "dismiss",
    "queue_item",
    "queue_items",
]
