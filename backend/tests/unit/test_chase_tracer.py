"""Proportional taint tracing (docs/10 §3, FR-CHS-01) on hand-built graphs."""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import given, settings
from hypothesis import strategies as st

from delphi.chase.tracer import (
    LedgerMove,
    SourcePayment,
    TraceResult,
    conservation_gap,
    trace,
)

MIN = 500_00  # ₹500 (kb/engine.yaml chase.min_trace_inr)
CASH = "CASH_OUT"


@dataclass
class Bank:
    """Builds a consistent ledger: every payment debits its payer and credits its payee."""

    balances: dict[str, int] = field(default_factory=dict)
    moves: list[LedgerMove] = field(default_factory=list)
    _ids: int = 0
    _now: int = 0

    def pay(self, payer: str, payee: str, amount: int, channel: str = "IMPS") -> str:
        self._now += 60_000_000
        payment = f"p{len(self.moves) // 2}"
        for account, delta in ((payer, -amount), (payee, amount)):
            self._ids += 1
            self.balances[account] = self.balances.get(account, 0) + delta
            self.moves.append(
                LedgerMove(
                    self._ids, self._now, payment, account, delta, self.balances[account], channel
                )
            )
        return payment

    def fund(self, account: str, amount: int) -> None:
        self.pay("payroll", account, amount)


def _run(bank: Bank, *sources: tuple[str, str, int]) -> TraceResult:
    result = trace(
        [SourcePayment(p, to, amount) for p, to, amount in sources],
        bank.moves,
        min_trace_paise=MIN,
        cash_out_channel=CASH,
    )
    assert conservation_gap(result) == 0
    return result


def test_a_mule_that_forwards_everything_passes_all_the_taint_on() -> None:
    bank = Bank()
    bank.fund("victim", 100_000_00)
    source = bank.pay("victim", "m1", 100_000_00)
    bank.pay("m1", "m2", 100_000_00)
    result = _run(bank, (source, "m1", 100_000_00))
    assert result.taint_now == {"m2": 100_000_00}
    assert result.depth == {"m1": 1, "m2": 2}
    assert [(f.from_account_id, f.to_account_id, f.taint_paise) for f in result.flows] == [
        ("m1", "m2", 100_000_00)
    ]


def test_mixed_funds_are_haircut_in_proportion() -> None:
    """docs/10 §3: τ_out = x · T / B, with B before the debit."""
    bank = Bank()
    bank.fund("m1", 10_000_00)  # the mule's own clean money
    bank.fund("victim", 10_000_00)
    source = bank.pay("victim", "m1", 10_000_00)
    bank.pay("m1", "m2", 5_000_00)  # T/B = 1/2 → ₹2,500 of taint goes on
    result = _run(bank, (source, "m1", 10_000_00))
    assert result.taint_now == {"m1": 7_500_00, "m2": 2_500_00}


def test_cash_out_is_leakage() -> None:
    bank = Bank()
    bank.fund("victim", 50_000_00)
    source = bank.pay("victim", "m1", 50_000_00)
    bank.pay("m1", "atm", 30_000_00, channel=CASH)
    result = _run(bank, (source, "m1", 50_000_00))
    assert result.leakage_paise == 30_000_00
    assert result.taint_now == {"m1": 20_000_00}  # the cash-out account holds no taint
    assert result.flows[-1].to_account_id is None


def test_small_remainders_are_pruned() -> None:
    bank = Bank()
    bank.fund("victim", 20_000_00)
    source = bank.pay("victim", "m1", 20_000_00)
    bank.pay("m1", "m2", 19_800_00)  # ₹200 left behind: below ₹500
    result = _run(bank, (source, "m1", 20_000_00))
    assert result.taint_now == {"m2": 19_800_00}
    assert result.pruned_paise == 200_00


def test_rounding_is_bankers_to_the_paisa() -> None:
    bank = Bank()
    bank.fund("m1", 2)  # B = 3 paise after the credit below, T = 1
    bank.fund("victim", 1_000_00)
    source = bank.pay("victim", "m1", 1)
    bank.pay("m1", "m2", 1)  # 1 · 1/3 = 0.33 → 0
    result = trace(
        [SourcePayment(source, "m1", 1)], bank.moves, min_trace_paise=0, cash_out_channel=CASH
    )
    assert result.taint_now == {"m1": 1} and conservation_gap(result) == 0


def test_a_payments_debit_comes_before_its_credit_whatever_the_entry_ids() -> None:
    bank = Bank()
    bank.fund("victim", 10_000_00)
    source = bank.pay("victim", "m1", 10_000_00)
    forward = bank.pay("m1", "m2", 10_000_00)
    swapped = [
        LedgerMove(-m.entry_id if m.payment_id == forward else m.entry_id, *_rest(m))
        for m in bank.moves
    ]
    result = trace(
        [SourcePayment(source, "m1", 10_000_00)],
        swapped,
        min_trace_paise=MIN,
        cash_out_channel=CASH,
    )
    assert result.taint_now == {"m2": 10_000_00}


def _rest(m: LedgerMove) -> tuple[int, str, str, int, int, str]:
    return (m.at_us, m.payment_id, m.account_id, m.delta_paise, m.balance_after_paise, m.channel)


def test_a_fan_out_reaches_every_layer_at_its_depth() -> None:
    bank = Bank()
    bank.fund("victim", 90_000_00)
    source = bank.pay("victim", "l1", 90_000_00)
    bank.pay("l1", "l2a", 45_000_00)
    bank.pay("l1", "l2b", 45_000_00)
    bank.pay("l2a", "l3", 45_000_00)
    result = _run(bank, (source, "l1", 90_000_00))
    assert result.depth == {"l1": 1, "l2a": 2, "l2b": 2, "l3": 3}
    assert result.taint_now == {"l2b": 45_000_00, "l3": 45_000_00}


def test_a_source_whose_credit_is_not_visible_still_taints_its_payee() -> None:
    result = trace(
        [SourcePayment("p", "m1", 1_000_00)], [], min_trace_paise=MIN, cash_out_channel=CASH
    )
    assert result.taint_now == {"m1": 1_000_00} and conservation_gap(result) == 0


@settings(max_examples=150, deadline=None)
@given(
    transfers=st.lists(
        st.tuples(st.integers(0, 5), st.integers(0, 5), st.integers(1, 100), st.booleans()),
        min_size=1,
        max_size=40,
    ),
    source_share=st.integers(1, 100),
    prune=st.sampled_from([0, 50, MIN]),
)
def test_taint_is_conserved_and_never_exceeds_the_balance(
    transfers: list[tuple[int, int, int, bool]], source_share: int, prune: int
) -> None:
    """The conservation property (docs/10 §3): Σ T_now + leakage + pruned = initial, exactly,
    and no account holds more taint than money."""
    bank = Bank()
    for account in range(6):
        bank.fund(f"a{account}", 1_000_00)
    bank.fund("victim", 1_000_00)
    amount = 10 * source_share
    source = bank.pay("victim", "a0", amount)
    for payer, payee, percent, cash in transfers:
        if payer == payee:
            continue
        x = bank.balances[f"a{payer}"] * percent // 100
        if x > 0:
            bank.pay(f"a{payer}", f"a{payee}", x, channel=CASH if cash else "IMPS")
    result = trace(
        [SourcePayment(source, "a0", amount)],
        bank.moves,
        min_trace_paise=prune,
        cash_out_channel=CASH,
    )
    assert conservation_gap(result) == 0
    for account, taint in result.taint_now.items():
        assert 0 < taint <= bank.balances[account]
