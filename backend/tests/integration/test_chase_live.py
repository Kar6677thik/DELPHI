"""Live mule movement and re-tracing with a virtual clock (docs/10 §5 step 5, docs/14 §8,
FR-CHS-08, #114): the simulator moves the ring's due transfers, honouring placed liens, and
``CHASE_RETRACE`` re-traces the open operation so the money's new holders become candidates."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, text

from delphi.api.security import Principal
from delphi.chase.api import TracedOnly, placed_lien_totals, retrace_due
from delphi.chase.tickets import ChaseRetraceJob
from delphi.platform.jobs import JobContext
from delphi.platform.locks import CHASE_RETRACE_LOCK_KEY, LIEN_ACCOUNT_LOCK_CLASS
from delphi.sim.tick import advance_transfers

from .test_cases import T0, Env, env, writer  # noqa: F401 - pytest fixtures
from .test_chase_liens import INR, _account, _post, _scam


def _schedule(e: Env, payer: UUID, payee: UUID, amount_inr: int, minutes: int) -> None:
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bank.sim_schedule (id, due_at, kind, payload, created_at)"
                " VALUES (:i, :d, 'TRANSFER', CAST(:p AS jsonb), :t)"
            ),
            {
                "i": uuid4(),
                "d": T0 + timedelta(minutes=minutes),
                "t": T0,
                "p": json.dumps(
                    {
                        "payer_account_id": str(payer),
                        "payee_account_id": str(payee),
                        "amount_paise": amount_inr * INR,
                        "channel": "IMPS",
                    }
                ),
            },
        )


def _balance(e: Env, account: UUID) -> int:
    with e.owner.connect() as conn:
        return int(
            conn.execute(
                text("SELECT balance_paise FROM bank.accounts WHERE id = :a"), {"a": account}
            ).scalar_one()
        )


def _tick(e: Env) -> list[Any]:
    with e.owner.begin() as conn:
        return advance_transfers(conn, e.clock.now(), held=placed_lien_totals)


def test_the_ring_moves_on_around_the_lien_and_the_retrace_follows(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    source, internal, external = _scam(env)
    op = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
    candidate = next(c for c in op["candidates"] if c["account_id"] == str(internal))
    _post(client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": [candidate["id"]]})

    onward, cash = _account(env, "DLPH", 0), _account(env, "XBK2", 0)
    _schedule(env, internal, onward, 80_000, minutes=5)  # all of it, but it is liened
    _schedule(env, external, cash, 50_000, minutes=5)
    _schedule(env, external, cash, 10_000, minutes=30)  # not due yet

    assert _tick(env) == []  # nothing is due at T0
    env.clock.advance(timedelta(minutes=10))
    moved = {m.payer_account_id: m for m in _tick(env)}
    assert moved[internal].moved_paise == 0  # the lien holds the whole balance
    assert moved[external].moved_paise == 50_000 * INR
    assert _balance(env, internal) == 80_000 * INR and _balance(env, cash) == 50_000 * INR
    assert _tick(env) == []  # each row runs once

    with env.sessions.begin() as s:
        assert retrace_due(s, env.clock, TracedOnly(), limit=10) == 1
    view = client.get(f"/api/v1/chase/operations/{op['id']}").json()
    taint = {c["account_id"]: c["taint_paise"] for c in view["candidates"]}
    assert taint[str(cash)] == 50_000 * INR  # the money's new holder is a candidate
    assert taint[str(external)] == 70_000 * INR
    with env.owner.connect() as conn:
        metrics = conn.execute(
            text("SELECT metrics FROM delphi.chase_operations WHERE id = :o"), {"o": op["id"]}
        ).scalar_one()
    assert metrics["golden_hour_recovery_ratio"] == 0.4  # ₹80k of ₹2L, within the hour
    assert metrics["amount_frozen_beyond_taint_paise"] == 0
    with env.sessions.begin() as s:  # not again before retrace_interval_min has passed
        assert retrace_due(s, env.clock, TracedOnly(), limit=10) == 0


def test_the_metrics_use_the_taint_at_approval(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """#258 review: a re-trace overwrites the candidates' taint; the metrics read each lien's
    taint at approval, so a later, lower taint doesn't make the lien look beyond its taint."""
    client, _ = writer
    source, internal, _external = _scam(env)
    op = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
    candidate = next(c for c in op["candidates"] if c["account_id"] == str(internal))
    _post(client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": [candidate["id"]]})

    class Lowered(TracedOnly):
        """The re-trace finds less of the money on the liened account, as if it had since
        been traced elsewhere, so the candidate's stored taint drops below the lien."""

        def candidates(self, *args: Any) -> Any:
            return [
                replace(c, taint_paise=1) if c.account_id == internal else c
                for c in super().candidates(*args)
            ]

    env.clock.advance(timedelta(minutes=10))
    with env.sessions.begin() as s:
        assert retrace_due(s, env.clock, Lowered(), limit=10) == 1
    view = client.get(f"/api/v1/chase/operations/{op['id']}").json()
    lowered = next(c for c in view["candidates"] if c["account_id"] == str(internal))
    assert lowered["taint_paise"] == 1  # the lower taint survived the re-trace
    with env.owner.connect() as conn:
        metrics = conn.execute(
            text("SELECT metrics FROM delphi.chase_operations WHERE id = :o"), {"o": op["id"]}
        ).scalar_one()
    # Read from the candidate, the lien would be ₹1.2L beyond its taint; from the lien's taint
    # at approval, it is within it (#258 CodeRabbit finding).
    assert metrics["amount_frozen_beyond_taint_paise"] == 0


def test_a_retrace_takes_at_most_its_limit_and_skips_while_busy(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """#258 review: due operations are picked in SQL, least recently traced first, at most the
    limit; the job skips while another run holds lock 56."""
    client, _ = writer
    for _ in range(2):
        source, _internal, _external = _scam(env)
        _post(client, "/chase/operations", {"payment_ids": [str(source)]})
    env.clock.advance(timedelta(minutes=10))
    with env.sessions.begin() as s:
        assert retrace_due(s, env.clock, TracedOnly(), limit=1) == 1
    with env.sessions.begin() as s:
        assert retrace_due(s, env.clock, TracedOnly(), limit=10) == 1
    job = ChaseRetraceJob(
        env.sessions,
        env.clock,
        lambda _s: None,
        tickets_per_run=10,
        retrace_batch=1,
        retrace_max_per_run=5,
    )
    ctx: Any = JobContext.__new__(JobContext)
    env.clock.advance(timedelta(minutes=10))
    with env.owner.begin() as held:
        held.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CHASE_RETRACE_LOCK_KEY})
        busy = job(ctx)
    assert busy["retraced"] == 0 and busy["retrace_skipped"] == "busy"
    assert job(ctx)["retraced"] == 2  # two batches of one


def test_a_retrace_publishes_its_events_after_every_trace(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """#258 review: the outbox lock is held from the first publish to the commit, so the batch
    publishes its CHASE_UPDATED events only once every operation is traced."""
    client, _ = writer
    for _ in range(2):
        source, _internal, _external = _scam(env)
        _post(client, "/chase/operations", {"payment_ids": [str(source)]})
    env.clock.advance(timedelta(minutes=10))
    seen: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    engine = env.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with env.sessions.begin() as s:
            assert retrace_due(s, env.clock, TracedOnly(), limit=10) == 2
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    traces = [i for i, q in enumerate(seen) if "chase_candidates" in q and "INSERT" in q]
    events = [i for i, q in enumerate(seen) if "outbox_events" in q and "INSERT" in q]
    assert traces and len(events) == 2
    assert min(events) > max(traces)


def test_the_tick_holds_its_payers_lien_locks(env: Env) -> None:  # noqa: F811
    """#258 review: SIM_TICK takes the per-account lien locks before reading the liens, so an
    approval racing it waits instead of setting a lien the transfer then moves past."""
    payer, payee = _account(env, "DLPH", 10_000), _account(env, "XBK2", 0)
    _schedule(env, payer, payee, 1_000, minutes=1)
    env.clock.advance(timedelta(minutes=2))
    with env.owner.begin() as conn:
        assert len(advance_transfers(conn, env.clock.now(), held=placed_lien_totals)) == 1
        with env.owner.connect() as other:
            free = other.execute(
                text("SELECT pg_try_advisory_lock(:k, hashtext(:a))"),
                {"k": LIEN_ACCOUNT_LOCK_CLASS, "a": str(payer)},
            ).scalar_one()
    assert free is False
