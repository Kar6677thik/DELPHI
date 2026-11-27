"""CFCFRMS tickets, expiry, release and closing on a real database (docs/10 §5 steps 4, 6, 7;
docs/14 §9; #113): an external lien waits on its ticket, is placed on acknowledgement for what
the account still holds, fails when the ticket expires, and an operation closes once its golden
hour has ended with no ticket pending."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from delphi.api.security import Principal
from delphi.chase.api import cfcfrms_tickets, chase_operations
from delphi.chase.tickets import (
    ChaseRetraceJob,
    close_due_operations,
    process_tickets,
    simulated_ack_delay,
)
from delphi.platform.jobs import JobContext
from delphi.platform.locks import LIEN_ACCOUNT_LOCK_CLASS

from .test_cases import Env, env, writer  # noqa: F401 - pytest fixtures
from .test_chase_liens import INR, _account, _pay, _post, _scam

ACK_AFTER = timedelta(minutes=20)


def _ticketed(client: TestClient, env: Env) -> tuple[dict[str, Any], UUID, str]:  # noqa: F811
    """An operation whose external lien was approved: (the view, the external mule, the ticket)."""
    source, _internal, external = _scam(env)
    op = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
    candidate = next(c for c in op["candidates"] if c["account_id"] == str(external))
    view = _post(
        client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": [candidate["id"]]}
    ).json()
    [ticket] = view["tickets"]
    return view, external, ticket["id"]


def _settle(env: Env, delay: timedelta | None) -> dict[str, int]:  # noqa: F811
    with env.sessions.begin() as s:
        return dict(process_tickets(s, env.clock, ack_delay=lambda _t: delay, limit=200))


def _lien(client: TestClient, op_id: str) -> dict[str, Any]:
    [lien] = client.get(f"/api/v1/chase/operations/{op_id}").json()["liens"]
    return lien


def test_an_acknowledged_ticket_places_the_lien(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    view, _external, _ticket = _ticketed(client, env)
    assert _settle(env, ACK_AFTER) == {"acknowledged": 0, "expired": 0}  # not yet
    env.clock.advance(ACK_AFTER)
    assert _settle(env, ACK_AFTER) == {"acknowledged": 1, "expired": 0}
    lien = _lien(client, view["id"])
    assert lien["status"] == "PLACED" and lien["amount_paise"] == 120_000 * INR
    assert lien["placed_at"] is not None and lien["review_due_at"] is not None
    [ticket] = client.get(f"/api/v1/chase/operations/{view['id']}").json()["tickets"]
    assert ticket["status"] == "ACKNOWLEDGED" and ticket["acknowledged_at"] is not None


def test_the_lien_follows_the_balance_at_acknowledgement(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """docs/10 §5 step 4: min(lien amount, the balance at that moment), or fail at 0."""
    client, _ = writer
    view, external, _ticket = _ticketed(client, env)
    _pay(env, external, _account(env, "XBK2", 0), 100_000, minutes=1)  # ₹20,000 left
    env.clock.advance(ACK_AFTER)
    _settle(env, ACK_AFTER)
    lien = _lien(client, view["id"])
    assert lien["status"] == "PLACED" and lien["amount_paise"] == 20_000 * INR

    view2, external2, _ = _ticketed(client, env)
    _pay(env, external2, _account(env, "XBK2", 0), 120_000, minutes=1)  # drained
    env.clock.advance(ACK_AFTER)
    _settle(env, ACK_AFTER)
    drained = _lien(client, view2["id"])
    assert drained["status"] == "FAILED_FUNDS_MOVED" and drained["amount_paise"] == 0


def test_an_unacknowledged_ticket_expires(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    view, _external, _ticket = _ticketed(client, env)
    env.clock.advance(timedelta(minutes=30))
    assert _settle(env, None) == {"acknowledged": 0, "expired": 0}
    env.clock.advance(timedelta(hours=3))  # past ticket_expiry_min
    assert _settle(env, None) == {"acknowledged": 0, "expired": 1}
    assert _lien(client, view["id"])["status"] == "FAILED_NO_RESPONSE"
    with env.owner.connect() as conn:
        status = conn.execute(select(cfcfrms_tickets.c.status)).scalar_one()
    assert status == "EXPIRED"


def test_the_operation_closes_after_the_golden_hour_with_nothing_pending(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    view, _external, _ticket = _ticketed(client, env)
    job = ChaseRetraceJob(env.sessions, env.clock, lambda _s: lambda _t: None, tickets_per_run=200)
    ctx: Any = JobContext.__new__(JobContext)
    env.clock.advance(timedelta(minutes=90))  # the golden hour is over, the ticket is pending
    assert job(ctx) == {"acknowledged": 0, "expired": 0, "closed": 0, "retraced": 1}
    env.clock.advance(timedelta(hours=3))
    assert job(ctx) == {"acknowledged": 0, "expired": 1, "closed": 1, "retraced": 0}
    with env.owner.connect() as conn:
        status = conn.execute(
            select(chase_operations.c.status).where(chase_operations.c.id == UUID(view["id"]))
        ).scalar_one()
    assert status == "CLOSED"


def test_release_and_manual_close(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    source, internal, _external = _scam(env)
    op = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
    candidate = next(c for c in op["candidates"] if c["account_id"] == str(internal))
    view = _post(
        client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": [candidate["id"]]}
    ).json()
    [lien] = view["liens"]
    path = f"/chase/operations/{op['id']}/liens/{lien['id']}/release"
    released = _post(client, path, {"reason": "Customer documents explain the credit."})
    assert released.status_code == 200, released.text
    released_lien = released.json()["liens"][0]
    assert released_lien["status"] == "RELEASED"
    assert released_lien["release_reason"] == "Customer documents explain the credit."
    assert released_lien["released_at"] is not None
    assert _post(client, path, {"reason": "again"}).status_code == 409
    closed = _post(client, f"/chase/operations/{op['id']}/close", {})
    assert closed.status_code == 200 and closed.json()["status"] == "CLOSED"
    assert _post(client, f"/chase/operations/{op['id']}/close", {}).status_code == 409
    approve = _post(
        client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": [candidate["id"]]}
    )
    assert approve.status_code == 409  # a closed operation takes no liens


def test_the_simulated_cfcfrms_needs_a_seeded_bank(env: Env) -> None:  # noqa: F811
    with env.sessions() as s:
        assert simulated_ack_delay(s) is None
    with env.owner.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM delphi.datasets")).scalar_one() == 0


def test_tickets_expire_with_no_seeded_dataset_a_capped_batch_at_a_time(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """No dataset, so no simulated CFCFRMS: nothing is acknowledged, but tickets still expire,
    at most ``tickets_per_run`` per run, the oldest sent first."""
    client, _ = writer
    first, _e1, first_ticket = _ticketed(client, env)
    env.clock.advance(timedelta(minutes=1))
    _second, _e2, second_ticket = _ticketed(client, env)
    job = ChaseRetraceJob(env.sessions, env.clock, simulated_ack_delay, tickets_per_run=1)
    ctx: Any = JobContext.__new__(JobContext)
    env.clock.advance(timedelta(hours=4))  # both past ticket_expiry_min
    assert job(ctx)["expired"] == 1
    with env.owner.connect() as conn:
        statuses = dict(conn.execute(select(cfcfrms_tickets.c.id, cfcfrms_tickets.c.status)).all())
    assert statuses == {UUID(first_ticket): "EXPIRED", UUID(second_ticket): "SENT_SIMULATED"}
    assert job(ctx)["expired"] == 1
    assert job(ctx)["expired"] == 0
    assert _lien(client, first["id"])["status"] == "FAILED_NO_RESPONSE"


def test_an_acknowledged_lien_leaves_room_for_the_accounts_other_liens(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """FR-CHS-05 at acknowledgement (#250 review). Ticket A is sent for ₹40,000; new money
    (₹20,000) reaches the mule and ticket B is sent for it; then the mule moves ₹30,000 out.
    Settled, A and B together hold at most the ₹30,000 left: A gets what B leaves."""
    client, _ = writer
    mule = _account(env, "XBK1", 0)
    sink = _account(env, "XBK2", 0)
    first = _pay(env, _account(env, "DLPH", 100_000, customer=True), mule, 40_000, minutes=50)
    statuses = []
    for n in range(2):
        source = (
            first
            if n == 0
            else _pay(env, _account(env, "DLPH", 100_000, customer=True), mule, 20_000, minutes=45)
        )
        op = _post(client, "/chase/operations", {"payment_ids": [str(source)]}).json()
        [candidate] = [c for c in op["candidates"] if c["account_id"] == str(mule)]
        view = _post(
            client, f"/chase/operations/{op['id']}/liens", {"candidate_ids": [candidate["id"]]}
        ).json()
        [lien] = view["liens"]
        statuses.append((lien["status"], lien["amount_paise"]))
    assert statuses == [("TICKET_SENT", 40_000 * INR), ("TICKET_SENT", 20_000 * INR)]
    _pay(env, mule, sink, 30_000, minutes=40)  # ₹30,000 left
    env.clock.advance(ACK_AFTER)
    assert _settle(env, ACK_AFTER) == {"acknowledged": 2, "expired": 0}
    with env.owner.connect() as conn:
        held = conn.execute(
            text(
                "SELECT coalesce(sum(amount_paise), 0) FROM delphi.liens"
                " WHERE account_id = :a AND status = 'PLACED'"
            ),
            {"a": mule},
        ).scalar_one()
    assert held == 30_000 * INR


def test_a_ticket_not_yet_due_holds_no_account_lock(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """#253 review: the ticket pass locks only the accounts of the tickets it acknowledges, so
    an approval on an account with a ticket still waiting is never blocked by it."""
    client, _ = writer
    _view, external, _ticket = _ticketed(client, env)
    with env.sessions.begin() as s:
        counts = process_tickets(s, env.clock, ack_delay=lambda _t: ACK_AFTER, limit=200)
        assert counts == {"acknowledged": 0, "expired": 0}
        with env.owner.connect() as other:
            free = other.execute(
                text("SELECT pg_try_advisory_lock(:k, hashtext(:a))"),
                {"k": LIEN_ACCOUNT_LOCK_CLASS, "a": str(external)},
            ).scalar_one()
            other.execute(
                text("SELECT pg_advisory_unlock(:k, hashtext(:a))"),
                {"k": LIEN_ACCOUNT_LOCK_CLASS, "a": str(external)},
            )
    assert free is True


def test_closing_skips_an_operation_another_transaction_holds(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """#253 review: the close step skips a locked operation instead of waiting for it (no
    deadlock with an approval), and closes it on the next run."""
    client, _ = writer
    view, _external, _ticket = _ticketed(client, env)
    env.clock.advance(timedelta(hours=4))  # golden hour over; the ticket expires
    assert _settle(env, None) == {"acknowledged": 0, "expired": 1}
    op_id = UUID(view["id"])
    with env.owner.begin() as held:
        held.execute(
            select(chase_operations.c.id).where(chase_operations.c.id == op_id).with_for_update()
        )
        with env.sessions.begin() as s:
            assert close_due_operations(s, env.clock, limit=10) == 0
    with env.sessions.begin() as s:
        assert close_due_operations(s, env.clock, limit=10) == 1
