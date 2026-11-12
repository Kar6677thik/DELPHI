"""Tracing a reported fraud through the bank-visible ledger (docs/10 §2–3, FR-CHS-01, NFR-04)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Connection, func, select

from delphi.chase.ledger import CASH_OUT_CHANNEL, load_trace_inputs, visible_until
from delphi.chase.tracer import LedgerMove, SourcePayment, TraceResult, conservation_gap, trace
from delphi.kb.model import KnowledgeBase
from delphi.sim import api as bank
from delphi.sim import truth
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import load_generator_config
from delphi.sim.writer import write_dataset

from ._db import engine_as, upgrade

NFR_04_SECONDS = 5.0
DEMO_CREATED_AT = datetime(2026, 11, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Traced:
    source: UUID
    mules: set[str]
    sources: list[SourcePayment]
    moves: list[LedgerMove]
    result: TraceResult
    elapsed_s: float
    full_scan: TraceResult  # the same trace over every ledger entry since the source
    every_fraud: tuple[int, float, int, int] | None = None  # (sources, s, entries, accounts)


def _full_scan_moves(conn: Connection, sources: list[SourcePayment], until: datetime) -> list:
    """Every visible ledger entry from the earliest source on: what the frontier read avoids."""
    e, p = bank.ledger_entries, bank.payments
    since = conn.execute(
        select(func.min(p.c.executed_at)).where(p.c.id.in_([s.payment_id for s in sources]))
    ).scalar_one()
    rows = conn.execute(
        select(
            e.c.id,
            e.c.at,
            e.c.payment_id,
            e.c.account_id,
            e.c.delta_paise,
            e.c.balance_after_paise,
            p.c.channel,
        )
        .join(p, p.c.id == e.c.payment_id)
        .where(e.c.at >= since, e.c.at < until)
    ).all()
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return [
        LedgerMove(
            r.id,
            (r.at - epoch) // timedelta(microseconds=1),
            str(r.payment_id),
            str(r.account_id),
            r.delta_paise,
            r.balance_after_paise,
            r.channel,
        )
        for r in rows
    ]


def _trace_a_layered_fraud(
    create_database: Callable[[], str],
    kb: KnowledgeBase,
    profile: str,
    *,
    every_fraud: bool = False,
) -> Traced:
    url = create_database()
    upgrade(url)
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:
        write_dataset(
            conn,
            generate_dataset(load_generator_config(), profile, kb=kb, created_at=DEMO_CREATED_AT),
        )
    # The test picks a fraud from the ground truth; the tracer itself sees only the bank.
    with sim.connect() as conn:
        p, pt, at = bank.payments, truth.payment_truth, truth.account_truth
        fwd, nxt = p.alias("fwd"), at.alias("nxt")
        mules = {
            str(m) for m in conn.execute(select(at.c.account_id).where(at.c.is_mule)).scalars()
        }
        # The largest executed fraud whose payee then layers it on to another mule (not every
        # fraud does: a mule may receive after its ring has finished moving money).
        source = conn.execute(
            select(p.c.id)
            .join(pt, pt.c.payment_id == p.c.id)
            .join(fwd, fwd.c.payer_account_id == p.c.payee_account_id)
            .join(nxt, nxt.c.account_id == fwd.c.payee_account_id)
            .where(
                pt.c.is_fraud,
                p.c.status == "EXECUTED",
                fwd.c.status == "EXECUTED",
                fwd.c.channel != CASH_OUT_CHANNEL,
                fwd.c.executed_at > p.c.executed_at,
                nxt.c.is_mule,
            )
            .order_by(p.c.amount_paise.desc(), p.c.id)
            .limit(1)
        ).scalar_one()
        frauds = (
            list(
                conn.execute(
                    select(p.c.id)
                    .join(pt, pt.c.payment_id == p.c.id)
                    .where(pt.c.is_fraud, p.c.status == "EXECUTED")
                ).scalars()
            )
            if every_fraud
            else []
        )
    sim.dispose()

    min_trace = int(kb.config.chase.min_trace_inr * 100)
    app = engine_as(url, "delphi_app")  # Chase runs with the bank's view only
    try:
        with app.connect() as conn:
            end = conn.execute(select(func.max(bank.ledger_entries.c.at))).scalar_one()
            until = visible_until(end + timedelta(seconds=1), 0)
            started = time.perf_counter()
            sources, moves = load_trace_inputs(conn, [source], until=until)
            result = trace(
                sources, moves, min_trace_paise=min_trace, cash_out_channel=CASH_OUT_CHANNEL
            )
            elapsed = time.perf_counter() - started
            full = trace(
                sources,
                _full_scan_moves(conn, sources, until),
                min_trace_paise=min_trace,
                cash_out_channel=CASH_OUT_CHANNEL,
            )
            every = None
            if frauds:  # the heavy case: every reported fraud of the dataset in one trace
                started = time.perf_counter()
                many, all_moves = load_trace_inputs(conn, frauds, until=until)
                big = trace(
                    many, all_moves, min_trace_paise=min_trace, cash_out_channel=CASH_OUT_CHANNEL
                )
                seconds = time.perf_counter() - started
                assert conservation_gap(big) == 0
                assert big == trace(
                    many,
                    _full_scan_moves(conn, many, until),
                    min_trace_paise=min_trace,
                    cash_out_channel=CASH_OUT_CHANNEL,
                )
                every = (len(many), seconds, len(all_moves), len(big.depth))
    finally:
        app.dispose()
    return Traced(source, mules, sources, moves, result, elapsed, full, every)


def _check(t: Traced) -> None:
    assert [s.payment_id for s in t.sources] == [str(t.source)]
    result = t.result
    assert conservation_gap(result) == 0
    assert result.initial_paise == t.sources[0].amount_paise > 0
    first_hop = t.sources[0].payee_account_id
    assert result.depth[first_hop] == 1 and first_hop in t.mules
    reached = set(result.depth)
    assert reached - {first_hop}, "the taint should move on from the first hop"
    assert len(reached & t.mules) >= 2
    # Reading outwards from the frontier gives exactly what scanning the whole ledger gives.
    assert result == t.full_scan
    assert t.elapsed_s < NFR_04_SECONDS


def test_a_reported_fraud_is_traced_into_the_mule_network(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    _check(_trace_a_layered_fraud(create_database, kb, "test"))


@pytest.mark.skipif(
    os.environ.get("DELPHI_RUN_SLOW") != "1", reason="slow: set DELPHI_RUN_SLOW=1 to run"
)
def test_nfr_04_on_the_demo_dataset(create_database: Callable[[], str], kb: KnowledgeBase) -> None:
    """NFR-04: a chase trace on the demo dataset takes 5 s or less."""
    traced = _trace_a_layered_fraud(create_database, kb, "demo", every_fraud=True)
    assert traced.every_fraud is not None
    n, seconds, entries, accounts = traced.every_fraud
    print(  # the timings are the PR's evidence
        f"demo, one fraud: {traced.elapsed_s:.3f} s, {len(traced.moves)} ledger entries read, "
        f"{len(traced.result.depth)} accounts reached; every fraud ({n} sources): "
        f"{seconds:.3f} s, {entries} entries, {accounts} accounts"
    )
    _check(traced)
    assert seconds < NFR_04_SECONDS


def test_nothing_to_trace(create_database: Callable[[], str]) -> None:
    url = create_database()
    upgrade(url)
    app = engine_as(url, "delphi_app")
    try:
        with app.connect() as conn:
            assert load_trace_inputs(conn, [UUID(int=1)], until=datetime.now(UTC)) == ([], [])
    finally:
        app.dispose()
