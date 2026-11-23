"""Live mule movement and re-tracing with a virtual clock (docs/10 §5 step 5, docs/14 §8,
FR-CHS-08, #114): the simulator moves the ring's due transfers, honouring placed liens, and
``CHASE_RETRACE`` re-traces the open operation so the money's new holders become candidates."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from delphi.api.security import Principal
from delphi.chase.api import TracedOnly, placed_lien_totals, retrace_due
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
        assert retrace_due(s, env.clock, TracedOnly()) == 1
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
        assert retrace_due(s, env.clock, TracedOnly()) == 0
