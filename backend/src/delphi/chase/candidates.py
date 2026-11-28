"""Scored freeze candidates (docs/10 §4, FR-CHS-02, FR-CHS-03, #112).

``Scored`` is the chase operation's ``CandidateSource``: for every account still holding at
least ``min_trace_inr`` of taint it loads what ``delphi.chase.scoring`` needs and stores the
scores and the ranking with the candidate.

- **Headroom:** the balance now less the account's active liens (PLACED, TICKET_SENT), from any
  operation: the most a new lien could hold (FR-CHS-05).
- **p_mule:** the mule scorer's score as of now (docs/13 §6), through ``MuleScorePort``.
- **Dwell time:** from the account's ledger over the last 30 days, as far as Chase can see it
  (docs/10 §2: up to ``now − visibility_lag``). The balance before the first entry in the
  window is the opening balance, which leaves first.
- **Latency:** an account at the home bank is liened internally; any other needs a ticket.

Like the tracer's loader, it reads the `bank` tables `delphi.sim.api` re-exports, never
`sim_truth` (FR-CHS-01).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from delphi.chase.ledger import to_us, visible_until
from delphi.chase.operations import (
    PAISE_PER_INR,
    TracedCandidate,
    active_liens_paise,
    visibility_lag,
)
from delphi.chase.scoring import (
    DWELL_WINDOW_DAYS,
    Candidate,
    Move,
    dwell_minutes,
    score,
    times_to_out,
)
from delphi.chase.tracer import TraceResult
from delphi.kb.model import KnowledgeBase
from delphi.sim import api as bank


class MuleScorePort(Protocol):
    """The mule scorer (docs/13 §6); the sensing layer's ``MuleScores`` in production."""

    def p_mule(self, account_id: UUID, as_of: datetime) -> float: ...


@dataclass(frozen=True, slots=True)
class _Ledger:
    """One account's visible moves in the dwell window, and its balance before them."""

    opening_paise: int
    moves: list[Move]


def _ledgers(
    session: Session, accounts: Sequence[UUID], *, since: datetime, until: datetime
) -> dict[UUID, _Ledger]:
    e = bank.ledger_entries.c
    rows = session.execute(
        select(e.account_id, e.at, e.delta_paise, e.balance_after_paise)
        .where(e.account_id.in_(list(accounts)), e.at >= since, e.at < until)
        .order_by(e.account_id, e.at, e.id)
    )
    opening: dict[UUID, int] = {}
    moves: dict[UUID, list[Move]] = defaultdict(list)
    for r in rows:
        opening.setdefault(r.account_id, r.balance_after_paise - r.delta_paise)
        moves[r.account_id].append(Move(to_us(r.at), r.delta_paise))
    return {a: _Ledger(opening[a], moves[a]) for a in opening}


@dataclass(frozen=True, slots=True)
class Scored:
    """The scored ``CandidateSource`` (docs/10 §4). Time runs REAL unless ``demo_time``."""

    mule: MuleScorePort
    demo_time: bool = False

    def candidates(
        self, session: Session, kb: KnowledgeBase, traced: TraceResult, now: datetime
    ) -> list[TracedCandidate]:
        chase = kb.config.chase
        floor = round(chase.min_trace_inr * PAISE_PER_INR)
        held = {UUID(a): t for a, t in traced.taint_now.items() if t >= floor}
        if not held:
            return []
        accounts = sorted(held, key=str)
        a = bank.accounts.c
        known = {
            r.id: r
            for r in session.execute(
                select(a.id, a.balance_paise, a.bank_code).where(a.id.in_(accounts))
            )
        }
        liened = active_liens_paise(session, accounts)
        until = visible_until(now, visibility_lag(session, kb))
        since = until - timedelta(days=DWELL_WINDOW_DAYS)
        until_us, since_us = to_us(until), to_us(since)
        ledgers = _ledgers(session, accounts, since=since, until=until)
        scale = kb.config.time.demo_time_scale if self.demo_time else 1.0
        balances: dict[UUID, int] = {}
        inputs: list[Candidate] = []
        for account in accounts:
            row = known.get(account)
            balances[account] = int(row.balance_paise) if row is not None else 0
            p_mule = self.mule.p_mule(account, now)
            ledger = ledgers.get(account, _Ledger(0, []))
            times = times_to_out(
                ledger.opening_paise, ledger.moves, since_us=since_us, now_us=until_us
            )
            inputs.append(
                Candidate(
                    account_id=str(account),
                    taint_paise=held[account],
                    balance_paise=balances[account] - liened.get(account, 0),  # the headroom
                    p_mule=p_mule,
                    internal=row is not None and row.bank_code == bank.HOME_BANK,
                    dwell_minutes=dwell_minutes(times, p_mule, chase, scale),
                )
            )
        return [
            TracedCandidate(
                account_id=UUID(s.account_id),
                hop=traced.depth[s.account_id],
                taint_paise=held[UUID(s.account_id)],
                balance_paise=balances[UUID(s.account_id)],
                p_present=s.p_present,
                p_mule=s.p_mule,
                erv_paise=s.erv_paise,
                wrongful_cost_paise=s.wrongful_paise,
                net_paise=s.net_paise,
                rank=s.rank,
                recommended=s.recommended,
            )
            for s in score(inputs, chase, scale)
        ]
