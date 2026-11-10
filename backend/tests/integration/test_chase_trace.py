"""Tracing a reported fraud through the bank-visible ledger (docs/10 §2–3, FR-CHS-01, NFR-04)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select

from delphi.chase.ledger import CASH_OUT_CHANNEL, load_trace_inputs, visible_until
from delphi.chase.tracer import conservation_gap, trace
from delphi.kb.model import KnowledgeBase
from delphi.sim import api as bank
from delphi.sim import truth
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import load_generator_config
from delphi.sim.writer import write_dataset

from ._db import engine_as, upgrade

NFR_04_SECONDS = 5.0


def test_a_reported_fraud_is_traced_into_the_mule_network(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    url = create_database()
    upgrade(url)
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:
        write_dataset(conn, generate_dataset(load_generator_config(), "test", kb=kb))
    # The test picks a fraud from the ground truth; the tracer itself sees only the bank.
    with sim.connect() as conn:
        p, pt, at = bank.payments, truth.payment_truth, truth.account_truth
        fwd, nxt = p.alias("fwd"), at.alias("nxt")
        mules = set(conn.execute(select(at.c.account_id).where(at.c.is_mule)).scalars())
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
    sim.dispose()

    app = engine_as(url, "delphi_app")  # Chase runs with the bank's view only
    try:
        with app.connect() as conn:
            end = conn.execute(select(func.max(bank.ledger_entries.c.at))).scalar_one()
            started = time.perf_counter()
            sources, moves = load_trace_inputs(
                conn, [source], until=visible_until(end + timedelta(seconds=1), 0)
            )
            result = trace(
                sources,
                moves,
                min_trace_paise=int(kb.config.chase.min_trace_inr * 100),
                cash_out_channel=CASH_OUT_CHANNEL,
            )
            elapsed = time.perf_counter() - started
    finally:
        app.dispose()

    assert [s.payment_id for s in sources] == [str(source)]
    assert conservation_gap(result) == 0
    assert result.initial_paise == sources[0].amount_paise > 0
    first_hop = sources[0].payee_account_id
    assert result.depth[first_hop] == 1 and first_hop in {str(m) for m in mules}
    reached = set(result.depth)
    assert reached - {first_hop}, "the taint should move on from the first hop"
    assert len({a for a in reached if a in {str(m) for m in mules}}) >= 2
    assert elapsed < NFR_04_SECONDS


def test_nothing_to_trace(create_database: Callable[[], str]) -> None:
    url = create_database()
    upgrade(url)
    app = engine_as(url, "delphi_app")
    try:
        with app.connect() as conn:
            assert load_trace_inputs(conn, [UUID(int=1)], until=datetime.now(UTC)) == ([], [])
    finally:
        app.dispose()
