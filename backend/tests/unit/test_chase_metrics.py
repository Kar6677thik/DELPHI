"""Chase metrics (docs/10 §6, FR-CHS-09, #114)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from delphi.chase.metrics import LienFacts, operation_metrics

T0 = datetime(2026, 11, 1, 10, tzinfo=UTC)
END = T0 + timedelta(hours=1)


def test_recovery_ratio_delay_and_the_beyond_taint_invariant() -> None:
    liens = [
        LienFacts(40_000, "PLACED", T0 + timedelta(minutes=12), 50_000),
        LienFacts(10_000, "RELEASED", T0 + timedelta(minutes=30), 10_000),  # placed first
        LienFacts(30_000, "PLACED", T0 + timedelta(minutes=90), 30_000),  # after the hour
        LienFacts(20_000, "TICKET_SENT", None, 20_000),  # not placed
        LienFacts(0, "FAILED_FUNDS_MOVED", None, 5_000),
    ]
    m = operation_metrics(
        reported_at=T0, golden_hour_ends_at=END, reported_loss_paise=100_000, liens=liens
    )
    assert m["golden_hour_recovery_ratio"] == 0.5  # (40k + 10k) / 100k
    assert m["tracing_delay_s"] == 12 * 60
    assert m["amount_frozen_beyond_taint_paise"] == 0


def test_no_placed_lien_and_a_lien_beyond_its_taint() -> None:
    m = operation_metrics(reported_at=T0, golden_hour_ends_at=END, reported_loss_paise=0, liens=[])
    assert m["golden_hour_recovery_ratio"] == 0.0 and m["tracing_delay_s"] is None
    over = operation_metrics(
        reported_at=T0,
        golden_hour_ends_at=END,
        reported_loss_paise=100,
        liens=[LienFacts(80, "PLACED", T0, 50)],
    )
    assert over["amount_frozen_beyond_taint_paise"] == 30  # the invariant check reports it
