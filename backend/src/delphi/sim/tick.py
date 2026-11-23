"""Live mule movement: ``SIM_TICK`` step 1 (docs/14 §8, docs/10 §5, FR-CHS-08, #114).

The generator leaves the mule rings' payments that fall after the history in
``bank.sim_schedule`` (TRANSFER rows, docs/14 §4). Each tick executes the rows now due, in due
order: the payment is written EXECUTED with both ledger entries, so Chase's tracer sees the money
move on. **A placed lien is honoured** (docs/14 §9, "external banks"): the mule can move only
what its account holds beyond the liened amount, so a transfer moves ``min(amount, balance −
held)`` and nothing when that is 0. The row is then done either way: the mule tries once.

The payment's id is the schedule row's, so a tick that dies before committing is redone exactly.
Runs as ``delphi_sim`` in the caller's transaction. Steps 2 (background payments, #101) and 3
(the CFCFRMS acknowledgements, done by ``CHASE_RETRACE``, #113) are not here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, select, update

from delphi.platform.clock import Clock
from delphi.platform.jobs import JobContext
from delphi.sim.bank import accounts, ledger_entries, payments, sim_schedule

TRANSFER = "TRANSFER"
EXECUTED = "EXECUTED"
LIVE = "SIM_LIVE"
BATCH = 500  # rows per tick: a long outage catches up over several ticks

HeldPaise = Callable[[Connection, Iterable[UUID]], Mapping[UUID, int]]
"""Each account's liened amount (the sum of its PLACED liens): Chase's, read by the caller."""


@dataclass(frozen=True, slots=True)
class Moved:
    schedule_id: UUID
    payer_account_id: UUID
    payee_account_id: UUID
    requested_paise: int
    moved_paise: int


def _balance(conn: Connection, account_id: UUID) -> int:
    value = conn.execute(
        select(accounts.c.balance_paise).where(accounts.c.id == account_id).with_for_update()
    ).scalar_one()
    return int(value)


def _move(conn: Connection, account_id: UUID, delta: int) -> int:
    a = accounts.c
    return int(
        conn.execute(
            update(accounts)
            .where(a.id == account_id)
            .values(balance_paise=a.balance_paise + delta, version=a.version + 1)
            .returning(a.balance_paise)
        ).scalar_one()
    )


def advance_transfers(conn: Connection, now: datetime, *, held: HeldPaise) -> list[Moved]:
    """Execute the TRANSFER rows due by ``now`` (oldest first, at most ``BATCH``)."""
    s = sim_schedule.c
    rows = conn.execute(
        select(s.id, s.due_at, s.payload)
        .where(s.kind == TRANSFER, s.done_at.is_(None), s.due_at <= now)
        .order_by(s.due_at, s.id)
        .limit(BATCH)
        .with_for_update(skip_locked=True)
    ).all()
    payers = {UUID(str(r.payload["payer_account_id"])) for r in rows}
    liened = dict(held(conn, payers)) if payers else {}
    moved: list[Moved] = []
    for r in rows:
        payer = UUID(str(r.payload["payer_account_id"]))
        payee = UUID(str(r.payload["payee_account_id"]))
        requested = int(r.payload["amount_paise"])
        available = _balance(conn, payer) - liened.get(payer, 0)
        amount = max(min(requested, available), 0)
        if amount > 0:
            conn.execute(
                payments.insert().values(
                    id=r.id,
                    payer_account_id=payer,
                    payee_account_id=payee,
                    amount_paise=amount,
                    channel=str(r.payload["channel"]),
                    initiated_at=r.due_at,
                    status=EXECUTED,
                    origin=LIVE,
                    executed_at=r.due_at,
                    created_at=now,
                )
            )
            for account, delta in ((payer, -amount), (payee, amount)):
                after = _move(conn, account, delta)
                conn.execute(
                    ledger_entries.insert().values(
                        account_id=account,
                        payment_id=r.id,
                        delta_paise=delta,
                        balance_after_paise=after,
                        at=r.due_at,
                    )
                )
        conn.execute(update(sim_schedule).where(s.id == r.id).values(done_at=now))
        moved.append(Moved(r.id, payer, payee, requested, amount))
    return moved


@dataclass(frozen=True, slots=True)
class SimTickJob:
    """``SIM_TICK`` (every 10 s, docs/04 §5.4): step 1, the mule network's due transfers."""

    sim_engine: Engine
    clock: Clock
    held: HeldPaise

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        with self.sim_engine.begin() as conn:
            moved = advance_transfers(conn, self.clock.now(), held=self.held)
        return {
            "transfers": len(moved),
            "held_back_paise": sum(m.requested_paise - m.moved_paise for m in moved),
        }
