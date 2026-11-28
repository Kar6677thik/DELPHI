"""Scored freeze candidates on a real database (docs/10 §4, FR-CHS-02, FR-CHS-03, #112): a chase
operation stores each candidate's p_present, p_mule, ERV, wrongful cost and net, ranked by net,
from the ledger, the active liens and the mule scorer."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from delphi.api.security import Principal
from delphi.chase.candidates import Scored

from .test_cases import Env, env, writer  # noqa: F401 - pytest fixtures
from .test_chase_liens import INR, _account, _pay, _post, _scam

# KB v1 `chase:` (kb/engine.yaml), restated so the expected values below are worked by hand.
KAPPA = 0.30
BASE_INR, RATE = 5_000, 0.02
INTERNAL_MIN, EXTERNAL_MIN = 1.0, 30.0  # the freeze latencies
MULE_DWELL_MIN, OTHER_DWELL_MIN = 20.0, 24 * 60.0  # the fallbacks under 3 credits


@dataclass
class FixedMule:
    """A mule scorer with the scores the test gives it."""

    scores: dict[UUID, float] = field(default_factory=dict)
    default: float = 0.9

    def p_mule(self, account_id: UUID, as_of: datetime) -> float:
        return self.scores.get(account_id, self.default)


def _scored(client: TestClient, mule: FixedMule) -> None:
    client.app.state.chase_candidates = Scored(mule)  # type: ignore[attr-defined]


def test_candidates_are_scored_and_ranked_by_net(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """The internal mule (p_mule 0.9, a 60 s lien) outranks the external account (p_mule 0.2, a
    30 min ticket), though the external one holds more taint. Both have under 3 credits, so the
    dwell time is the KB fallback (20 min for a likely mule, else 24 h)."""
    client, _ = writer
    source, internal, external = _scam(env)
    _scored(client, FixedMule({internal: 0.9, external: 0.2}))
    created = _post(client, "/chase/operations", {"payment_ids": [str(source)]})
    assert created.status_code == 201, created.text
    first, second = created.json()["candidates"]

    assert (first["account_id"], first["rank"]) == (str(internal), 1)
    present = math.exp(-INTERNAL_MIN / MULE_DWELL_MIN)
    erv = round(80_000 * INR * present * (0.9 + 0.1 * KAPPA))
    wrongful = round(0.1 * (BASE_INR + RATE * 80_000) * INR)
    assert wrongful == 660 * INR  # 10% of (₹5,000 + 2% of ₹80,000)
    assert first["p_present"] == pytest.approx(present)
    assert first["p_mule"] == 0.9
    assert (first["erv_paise"], first["wrongful_cost_paise"]) == (erv, wrongful)
    assert first["net_paise"] == erv - wrongful and first["recommended"] is True
    assert (first["taint_paise"], first["balance_paise"]) == (80_000 * INR, 80_000 * INR)

    assert (second["account_id"], second["rank"]) == (str(external), 2)
    present = math.exp(-EXTERNAL_MIN / OTHER_DWELL_MIN)
    erv = round(120_000 * INR * present * (0.2 + 0.8 * KAPPA))
    wrongful = round(0.8 * (BASE_INR + RATE * 120_000) * INR)
    assert second["p_present"] == pytest.approx(present)
    assert (second["erv_paise"], second["wrongful_cost_paise"]) == (erv, wrongful)
    assert second["net_paise"] == erv - wrongful and second["recommended"] is True
    assert first["net_paise"] > second["net_paise"]


def test_the_dwell_time_comes_from_the_ledger(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """Three earlier credits each left after 10 minutes; the traced one is still there after 50.
    The median time to 80% out is 10 min, so τ = 10 / ln 2 and p_present = 2^(−1/10) for a 60 s
    lien."""
    client, _ = writer
    feeder = _account(env, "DLPH", 100_000, customer=True)
    mule = _account(env, "DLPH", 0)
    sink = _account(env, "XBK1", 0)
    for arrived in (300, 200, 100):
        _pay(env, feeder, mule, 1_000, minutes=arrived)
        _pay(env, mule, sink, 1_000, minutes=arrived - 10)
    source = _pay(env, feeder, mule, 40_000, minutes=50)
    _scored(client, FixedMule())
    op = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
    [candidate] = op["candidates"]
    assert candidate["account_id"] == str(mule)
    assert candidate["p_present"] == pytest.approx(2 ** (-INTERNAL_MIN / 10))


def test_money_already_under_a_lien_is_not_counted_again(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """docs/10 §4: lien_amount = min(T_now, headroom). Once a first operation's lien holds the
    mule's whole balance, a second operation over the same money has nothing left to recover:
    ERV 0, only the wrongful cost, so the candidate is not recommended."""
    client, _ = writer
    mule = _account(env, "DLPH", 0)
    source = _pay(env, _account(env, "DLPH", 100_000, customer=True), mule, 40_000, minutes=50)
    _scored(client, FixedMule(default=0.6))
    first = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
    [candidate] = first["candidates"]
    assert candidate["recommended"] is True
    approved = _post(
        client, f"/chase/operations/{first['id']}/liens", {"candidate_ids": [candidate["id"]]}
    )
    assert approved.status_code == 200, approved.text

    second = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
    [again] = second["candidates"]
    assert again["balance_paise"] == 40_000 * INR  # the balance itself is unchanged
    assert again["erv_paise"] == 0
    assert again["wrongful_cost_paise"] == round(0.4 * BASE_INR * INR)
    assert again["net_paise"] < 0 and again["recommended"] is False
