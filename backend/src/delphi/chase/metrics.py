"""Chase metrics per operation (docs/10 §6, FR-CHS-09, #114). Pure.

- ``golden_hour_recovery_ratio`` = Σ lien amounts PLACED within the golden hour of
  ``reported_at`` ÷ the reported loss;
- ``tracing_delay_s`` = the first PLACED lien's time − ``reported_at`` (None before one);
- ``amount_frozen_beyond_taint`` must always be 0: the liens over the candidates' traced taint at
  approval (an invariant check, FR-CHS-05).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

PLACED = frozenset({"PLACED", "RELEASED"})  # a released lien was placed first


@dataclass(frozen=True, slots=True)
class LienFacts:
    amount_paise: int
    status: str
    placed_at: datetime | None
    taint_at_approval_paise: int


def operation_metrics(
    *,
    reported_at: datetime,
    golden_hour_ends_at: datetime,
    reported_loss_paise: int,
    liens: Iterable[LienFacts],
) -> Mapping[str, Any]:
    placed = [x for x in liens if x.status in PLACED and x.placed_at is not None]
    in_hour = sum(
        x.amount_paise for x in placed if x.placed_at and x.placed_at <= golden_hour_ends_at
    )
    first = min((x.placed_at for x in placed if x.placed_at is not None), default=None)
    beyond = sum(max(x.amount_paise - x.taint_at_approval_paise, 0) for x in placed)
    return {
        "golden_hour_recovery_ratio": in_hour / reported_loss_paise if reported_loss_paise else 0.0,
        "recovered_in_golden_hour_paise": in_hour,
        "tracing_delay_s": (first - reported_at).total_seconds() if first is not None else None,
        "amount_frozen_beyond_taint_paise": beyond,
    }
