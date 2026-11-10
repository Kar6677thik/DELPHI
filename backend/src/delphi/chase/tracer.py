"""Proportional (haircut) taint tracing (docs/10 §3, FR-CHS-01).

Each account carries a balance ``B`` and a taint ``T`` (0 ≤ T ≤ B). The source payments taint
their payees with their amounts; then every ledger movement since the earliest source is
applied in time order:

- a credit of ``x`` carrying taint ``τ``: ``B += x``, ``T += τ``;
- a debit of ``x``: ``τ_out = round(x · T / B)``, with ``B`` before the debit, banker's rounding
  to the paisa, 0 when ``B`` is 0. ``T −= τ_out``, and the payment carries ``τ_out`` to its payee;
- a debit on the cash-out channel: the taint leaves the banking system (cash-out leakage);
- an account whose taint drops below ``min_trace_paise`` is no longer followed: what it held is
  recorded as pruned.

Taint is moved in whole paise, so it is conserved exactly:
``Σ T_now + leakage + pruned = initial`` (a property test checks it).

Balances come from the ledger itself (``balance_after_paise − delta_paise`` is the balance before
each entry), so an account first reached mid-way is traced with its real balance. A payment's
debit is always applied before its credit, whatever their entry ids. The tracer is pure: the
caller loads the ledger the bank can see (``delphi.chase.ledger``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import groupby


@dataclass(frozen=True, slots=True)
class LedgerMove:
    """One ledger entry: ``delta_paise`` > 0 credits ``account_id``, < 0 debits it."""

    entry_id: int
    at_us: int
    payment_id: str
    account_id: str
    delta_paise: int
    balance_after_paise: int
    channel: str


@dataclass(frozen=True, slots=True)
class SourcePayment:
    """A reported payment: victim → first-hop account."""

    payment_id: str
    payee_account_id: str
    amount_paise: int


@dataclass(frozen=True, slots=True)
class TaintFlow:
    """Taint carried by one payment, for the chase graph."""

    payment_id: str
    from_account_id: str
    to_account_id: str | None  # None: cashed out
    at_us: int
    taint_paise: int


@dataclass(slots=True)
class TraceResult:
    initial_paise: int
    taint_now: dict[str, int] = field(default_factory=dict)  # accounts still holding taint
    flows: list[TaintFlow] = field(default_factory=list)
    leakage_paise: int = 0  # cashed out
    pruned_paise: int = 0  # below the trace threshold, no longer followed
    depth: dict[str, int] = field(default_factory=dict)  # first-hop accounts are 1

    @property
    def traced_paise(self) -> int:
        return sum(self.taint_now.values())


def _haircut(amount: int, taint: int, balance: int) -> int:
    if balance <= 0 or taint <= 0:
        return 0
    return min(round(amount * taint / balance), taint)  # round(): banker's rounding


def trace(
    sources: Iterable[SourcePayment],
    moves: Iterable[LedgerMove],
    *,
    min_trace_paise: int,
    cash_out_channel: str,
) -> TraceResult:
    """Follow the taint of ``sources`` through ``moves`` (any order; sorted here)."""
    carried: dict[str, int] = {}  # payment → taint it carries to its payee's credit
    source_payees: dict[str, str] = {}
    result = TraceResult(initial_paise=0)
    for source in sources:
        carried[source.payment_id] = carried.get(source.payment_id, 0) + source.amount_paise
        source_payees[source.payment_id] = source.payee_account_id
        result.initial_paise += source.amount_paise
        result.depth.setdefault(source.payee_account_id, 1)
    sources_seen: set[str] = set()
    taint = result.taint_now

    def settle(account: str) -> None:
        held = taint.get(account, 0)
        if 0 < held < min_trace_paise:
            result.pruned_paise += held
            del taint[account]
        elif held == 0:
            taint.pop(account, None)

    ordered = sorted(moves, key=lambda m: (m.at_us, m.entry_id))
    first_at: dict[str, tuple[int, int]] = {}
    for m in ordered:
        first_at.setdefault(m.payment_id, (m.at_us, m.entry_id))
    by_payment = sorted(ordered, key=lambda m: (*first_at[m.payment_id], m.delta_paise > 0))
    for payment_id, group in groupby(by_payment, key=lambda m: m.payment_id):
        entries = list(group)
        debit = next((m for m in entries if m.delta_paise < 0), None)
        credit = next((m for m in entries if m.delta_paise > 0), None)
        if debit is not None and taint.get(debit.account_id, 0) > 0:
            amount = -debit.delta_paise
            balance = debit.balance_after_paise + amount
            out = _haircut(amount, taint[debit.account_id], balance)
            if out:
                taint[debit.account_id] -= out
                to = credit.account_id if credit is not None else None
                cashed = debit.channel == cash_out_channel or to is None
                result.flows.append(
                    TaintFlow(
                        payment_id, debit.account_id, None if cashed else to, debit.at_us, out
                    )
                )
                if cashed:
                    result.leakage_paise += out
                else:
                    carried[payment_id] = carried.get(payment_id, 0) + out
                    assert to is not None
                    depth = result.depth.get(debit.account_id, 1) + 1
                    result.depth[to] = min(result.depth.get(to, depth), depth)
            settle(debit.account_id)
        if credit is not None and payment_id in carried:
            arriving = carried.pop(payment_id)
            if payment_id in source_payees:
                sources_seen.add(payment_id)
            taint[credit.account_id] = taint.get(credit.account_id, 0) + arriving
            settle(credit.account_id)
    # A source whose credit is not in the visible ledger still taints its payee.
    for payment_id, payee in source_payees.items():
        if payment_id not in sources_seen and payment_id in carried:
            taint[payee] = taint.get(payee, 0) + carried.pop(payment_id)
            settle(payee)
    return result


def conservation_gap(result: TraceResult) -> int:
    """``initial − (Σ T_now + leakage + pruned)``: 0 when taint is conserved."""
    return result.initial_paise - (result.traced_paise + result.leakage_paise + result.pruned_paise)
