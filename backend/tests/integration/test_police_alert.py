"""The coercion protocol's police alert (docs/09 §6, FR-NAV-10, #106): drafted when a coercion
case opens, and sent or discarded only by an investigator's explicit call."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from delphi.api.security import Principal
from delphi.llm.minimise import pseudonym
from delphi.navigator.api import CaseRecord
from delphi.navigator.tables import police_alert_drafts
from delphi.platform.tables import audit_log

from .test_cases import BROWSER, _held_case, env, writer  # noqa: F401 - pytest fixtures
from .test_guardian_store import Env


def _act(client: TestClient, case: CaseRecord, action: str) -> Any:
    return client.post(f"/api/v1/cases/{case.id}/police-alert/{action}", headers=BROWSER)


def _payer(e: Env, case: CaseRecord) -> Any:
    with e.owner.connect() as conn:
        return conn.execute(
            text(
                "SELECT a.customer_id FROM bank.payments p"
                " JOIN bank.accounts a ON a.id = p.payer_account_id WHERE p.id = :p"
            ),
            {"p": case.subject_payment_id},
        ).scalar_one()


def _status(e: Env, case: CaseRecord) -> str:
    with e.owner.connect() as conn:
        return str(
            conn.execute(
                select(police_alert_drafts.c.status).where(police_alert_drafts.c.case_id == case.id)
            ).scalar_one()
        )


def test_a_coercion_case_drafts_an_alert_that_nothing_sends_by_itself(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """FR-NAV-10: the draft stays DRAFT through reads, evidence and other activity; only the
    human SEND call changes it."""
    client, principal = writer
    case = _held_case(env, "PROTECTIVE_HOLD")
    view = client.get(f"/api/v1/cases/{case.id}").json()
    alert = view["police_alert"]
    assert alert["status"] == "DRAFT" and alert["acted_by"] is None
    customer = _payer(env, case)
    # docs/15 §6: the same CUST-… pseudonym as the LLM payloads, never the name or the id.
    assert alert["payload"]["victim"] == pseudonym("CUST", customer)
    assert alert["payload"]["amount_paise"] > 0
    assert str(customer) not in str(alert) and "display_name" not in str(alert)
    headers = {"Idempotency-Key": str(uuid4()), **BROWSER}
    fetched = client.post(
        f"/api/v1/cases/{case.id}/evidence-requests",
        json={"source_key": "ACCOUNT_PROFILE"},
        headers=headers,
    )
    assert fetched.status_code == 200
    assert _status(env, case) == "DRAFT"  # evidence does not send it
    before = client.get(f"/api/v1/cases/{case.id}").json()["version"]
    sent = _act(client, case, "send")
    assert sent.status_code == 200, sent.text
    assert sent.json()["version"] == before + 1  # the alert changed the case view (#195 review)
    assert sent.headers["etag"] == f'"{before + 1}"'  # with its new ETag (docs/16 §1)
    alert = sent.json()["police_alert"]
    assert alert["status"] == "SENT_SIMULATED" and alert["acted_by"] == str(principal.user_id)
    assert [e["source_key"] for e in alert["payload"]["evidence"]] == ["ACCOUNT_PROFILE"]
    with env.owner.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).where(audit_log.c.action == "police_alert.sent")
            ).scalar_one()
            == 1
        )
    again = _act(client, case, "send")
    assert again.status_code == 409 and again.json()["code"] == "INVALID_STATE_TRANSITION"
    assert _act(client, case, "discard").status_code == 409


def test_an_alert_can_be_discarded_and_a_standard_case_has_none(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    coercion = _held_case(env, "PROTECTIVE_HOLD")
    discarded = _act(client, coercion, "discard")
    assert discarded.status_code == 200
    assert discarded.json()["police_alert"]["status"] == "DISCARDED"
    assert discarded.json()["version"] == coercion.version + 1
    assert discarded.headers["etag"] == f'"{coercion.version + 1}"'
    assert _status(env, coercion) == "DISCARDED"
    standard = _held_case(env, "HOLD_FOR_REVIEW")
    assert client.get(f"/api/v1/cases/{standard.id}").json()["police_alert"] is None
    assert _act(client, standard, "send").status_code == 404
    assert _act(client, coercion, "publish").status_code == 422  # not an action
