"""FR-CHS-05 as a property (docs/05 §6, #113): a lien is never more than the taint traced to the
account, never more than its balance when the lien is approved, and never the whole balance
unless the whole balance is taint."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from delphi.chase import operations
from delphi.chase.operations import lien_amount
from delphi.chase.tracer import SourcePayment, trace
from delphi.kb.model import KnowledgeBase

from .test_chase_tracer import CASH, MIN, Bank


@settings(max_examples=200, deadline=None)
@given(
    transfers=st.lists(
        st.tuples(st.integers(0, 5), st.integers(0, 5), st.integers(1, 100)),
        min_size=1,
        max_size=30,
    ),
    source_share=st.integers(1, 100),
    later=st.lists(st.integers(-100, 100), min_size=6, max_size=6),
)
def test_a_lien_is_at_most_the_taint_and_the_balance(
    transfers: list[tuple[int, int, int]], source_share: int, later: list[int]
) -> None:
    bank = Bank()
    for account in range(6):
        bank.fund(f"a{account}", 1_000_00)
    bank.fund("victim", 1_000_00)
    amount = 10 * source_share
    source = bank.pay("victim", "a0", amount)
    for payer, payee, percent in transfers:
        x = bank.balances[f"a{payer}"] * percent // 100
        if payer != payee and x > 0:
            bank.pay(f"a{payer}", f"a{payee}", x)
    traced = trace(
        [SourcePayment(source, "a0", amount)], bank.moves, min_trace_paise=0, cash_out_channel=CASH
    )
    for i, (account, taint) in enumerate(sorted(traced.taint_now.items())):
        # The money keeps moving between the trace and the approval (docs/10 §5 step 2).
        change = later[i % len(later)]
        balance_now = max(bank.balances[account] + change * bank.balances[account] // 100, 0)
        lien = lien_amount(taint, balance_now)
        assert 0 <= lien <= taint and lien <= balance_now
        if taint < balance_now:
            assert lien < balance_now  # never the whole balance unless it is all taint


def test_the_floor_and_the_edges() -> None:
    assert lien_amount(MIN, 0) == 0
    assert lien_amount(0, 1_000) == 0
    assert lien_amount(700, 500) == 500
    assert lien_amount(300, 500) == 300


def test_the_visibility_lag_follows_the_dataset_profile(
    monkeypatch: pytest.MonkeyPatch, kb: KnowledgeBase
) -> None:
    lags = kb.config.chase.visibility_lag_min
    for profile, lag in (("eval", lags.eval), ("demo", lags.demo), ("test", lags.demo)):
        monkeypatch.setattr(
            operations.bank, "current_dataset", lambda _s, p=profile: SimpleNamespace(profile=p)
        )
        assert operations.visibility_lag(cast(Session, None), kb) == lag
    monkeypatch.setattr(operations.bank, "current_dataset", lambda _s: None)
    assert operations.visibility_lag(cast(Session, None), kb) == lags.demo
