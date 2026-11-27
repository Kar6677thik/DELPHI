"""Chase operations and liens over HTTP on a real database (docs/10 §5, docs/16 "Chase", #113):
an operation traces the reported money and stores its candidates, and **no lien exists until an
investigator calls the approve endpoint** (FR-CHS-04). An internal lien is placed at once for the
taint still traced (≤ the balance, FR-CHS-05); an external one sends a CFCFRMS ticket."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from delphi.api.security import Principal, current_principal
from delphi.chase.api import cfcfrms_tickets, liens

from .test_cases import BROWSER, T0, VIEWER, Env, env, writer  # noqa: F401 - pytest fixtures

INR = 100


def _account(e: Env, bank_code: str, balance_inr: int, *, customer: bool = False) -> UUID:
    account, owner = uuid4(), uuid4() if customer else None
    with e.owner.begin() as conn:
        if owner is not None:
            conn.execute(
                text(
                    "INSERT INTO bank.customers (id, display_name, age, language, is_vulnerable,"
                    " segment, income_band, consent_call_status, home_city)"
                    " VALUES (:c, 'CUST', 70, 'te', true, 'RETAIL', 'MID', true, 'Vijayawada')"
                ),
                {"c": owner},
            )
        conn.execute(
            text(
                "INSERT INTO bank.accounts (id, customer_id, bank_code, opened_at, kyc_level,"
                " status, balance_paise) VALUES (:a, :c, :b, :t, 'FULL', 'ACTIVE', :m)"
            ),
            {
                "a": account,
                "c": owner,
                "b": bank_code,
                "t": T0 - timedelta(days=400),
                "m": balance_inr * INR,
            },
        )
    return account


def _pay(e: Env, payer: UUID, payee: UUID, amount_inr: int, minutes: int) -> UUID:
    """An executed payment with both ledger entries, ``minutes`` before now."""
    payment, at = uuid4(), T0 - timedelta(minutes=minutes)
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bank.payments (id, payer_account_id, payee_account_id, amount_paise,"
                " channel, initiated_at, status, origin, executed_at)"
                " VALUES (:p, :a, :b, :m, 'IMPS', :t, 'EXECUTED', 'SIM_LIVE', :t)"
            ),
            {"p": payment, "a": payer, "b": payee, "m": amount_inr * INR, "t": at},
        )
        for account, delta in ((payer, -amount_inr * INR), (payee, amount_inr * INR)):
            conn.execute(
                text("UPDATE bank.accounts SET balance_paise = balance_paise + :d WHERE id = :a"),
                {"d": delta, "a": account},
            )
            conn.execute(
                text(
                    "INSERT INTO bank.ledger_entries (account_id, payment_id, delta_paise,"
                    " balance_after_paise, at) SELECT :a, :p, :d, balance_paise, :t"
                    " FROM bank.accounts WHERE id = :a"
                ),
                {"a": account, "p": payment, "d": delta, "t": at},
            )
    return payment


def _scam(e: Env) -> tuple[UUID, UUID, UUID]:
    """A victim pays ₹2,00,000 to an internal mule, which sends ₹1,20,000 on to an external
    one. Returns (the source payment, the internal mule, the external mule)."""
    victim = _account(e, "DLPH", 300_000, customer=True)
    internal = _account(e, "DLPH", 0)
    external = _account(e, "XBK1", 0)
    source = _pay(e, victim, internal, 200_000, minutes=40)
    _pay(e, internal, external, 120_000, minutes=30)
    return source, internal, external


def _post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    headers = {"Idempotency-Key": str(uuid4()), **BROWSER}
    return client.post(f"/api/v1{path}", json=body, headers=headers)


def _liens(e: Env) -> int:
    with e.owner.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(liens)).scalar_one())


def test_no_lien_without_the_approve_call(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, principal = writer
    source, internal, external = _scam(env)
    created = _post(client, "/chase/operations", {"payment_ids": [str(source)]})
    assert created.status_code == 201, created.text
    op = created.json()
    assert op["reported_loss_paise"] == 200_000 * INR
    taint = {c["account_id"]: c["taint_paise"] for c in op["candidates"]}
    assert taint == {str(internal): 80_000 * INR, str(external): 120_000 * INR}
    assert op["liens"] == [] and _liens(env) == 0  # tracing alone places nothing

    # A VIEWER cannot approve (docs/10 §5 step 1).
    client.app.dependency_overrides[current_principal] = lambda: VIEWER  # type: ignore[attr-defined]
    ids = [c["id"] for c in op["candidates"]]
    assert (
        _post(client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": ids}).status_code
        == 403
    )
    assert _liens(env) == 0

    client.app.dependency_overrides[current_principal] = lambda: principal  # type: ignore[attr-defined]
    approved = _post(client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": ids})
    assert approved.status_code == 200, approved.text
    by_account = {lien["account_id"]: lien for lien in approved.json()["liens"]}
    placed = by_account[str(internal)]
    assert placed["status"] == "PLACED" and placed["amount_paise"] == 80_000 * INR
    assert placed["approved_by"] == str(principal.user_id)
    assert placed["review_due_at"] is not None and placed["evidence_refs"]
    assert "Traced ₹80,000.00" in placed["recorded_reasons"]
    ticketed = by_account[str(external)]
    assert ticketed["status"] == "TICKET_SENT" and ticketed["amount_paise"] == 120_000 * INR
    [ticket] = approved.json()["tickets"]
    assert ticket["status"] == "SENT_SIMULATED" and ticket["target_bank"] == "XBK1"
    with env.owner.connect() as conn:
        assert conn.execute(select(func.count()).select_from(cfcfrms_tickets)).scalar_one() == 1

    again = _post(client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": ids[:1]})
    assert again.status_code == 409  # one lien per candidate


def test_the_amount_is_recomputed_at_approval(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """The mule moved the money on between the trace and the approval: the lien follows the
    taint still there, and a drained account fails (docs/10 §5 steps 2–3)."""
    client, _ = writer
    source, internal, _external = _scam(env)
    op = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
    candidate = next(c for c in op["candidates"] if c["account_id"] == str(internal))
    cash = _account(env, "DLPH", 0)
    _pay(env, internal, cash, 80_000, minutes=1)  # drained before the approval
    approved = _post(
        client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": [candidate["id"]]}
    ).json()
    [lien] = approved["liens"]
    assert lien["status"] == "FAILED_FUNDS_MOVED" and lien["amount_paise"] == 0


def test_an_operation_needs_executed_money(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    response = _post(client, "/chase/operations", {"payment_ids": [str(uuid4())]})
    assert response.status_code == 422
    assert _post(client, "/chase/operations", {}).status_code == 422


def test_liens_from_two_operations_stay_within_the_balance(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """FR-CHS-05 across operations (#250 review): two operations trace the same ₹40,000 to a
    mule holding ₹40,000 (a complaint and a payment-id operation over one payment, say). Each
    alone would lien min(taint, balance) = ₹40,000, ₹80,000 in all; the second gets only what
    the first left, nothing here."""
    client, _ = writer
    mule = _account(env, "DLPH", 0)
    source = _pay(env, _account(env, "DLPH", 100_000, customer=True), mule, 40_000, minutes=50)
    placed = []
    for _ in range(2):
        op = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
        [candidate] = [c for c in op["candidates"] if c["account_id"] == str(mule)]
        view = _post(
            client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": [candidate["id"]]}
        )
        assert view.status_code == 200, view.text
        assert view.headers["ETag"] == f'"{view.json()["version"]}"'
        [lien] = view.json()["liens"]
        placed.append((lien["status"], lien["amount_paise"]))
    assert placed == [("PLACED", 40_000 * INR), ("FAILED_FUNDS_MOVED", 0)]
