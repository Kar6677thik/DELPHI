"""Investigator trace events (docs/09 §5, FR-NAV-08, #108): the server records the actions in
their own transactions, and the console's view events arrive through
`POST /cases/{id}/trace-events`."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from delphi.api.security import Principal
from delphi.navigator.api import CaseRecord, get_case, list_traces
from delphi.navigator.tables import trace_events

from .test_cases import (  # noqa: F401 - pytest fixtures
    BROWSER,
    T0,
    _dispose,
    _held_case,
    env,
    writer,
)
from .test_guardian_store import Env


def _traces(e: Env, case: CaseRecord) -> list[dict[str, Any]]:
    with e.sessions() as s:
        return [dict(t) for t in list_traces(s, case.id)]


def _post(client: TestClient, case_id: object, events: list[dict[str, Any]]) -> Any:
    return client.post(
        f"/api/v1/cases/{case_id}/trace-events", json={"events": events}, headers=BROWSER
    )


def _fetch(client: TestClient, case: CaseRecord, source_key: str) -> Any:
    headers = {"Idempotency-Key": str(uuid4()), **BROWSER}
    return client.post(
        f"/api/v1/cases/{case.id}/evidence-requests",
        json={"source_key": source_key},
        headers=headers,
    )


def test_the_server_records_opening_requests_and_the_disposition(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, principal = writer
    case = _held_case(env)
    shown = client.get(f"/api/v1/cases/{case.id}").json()["next_best_evidence"]
    row = next(r for r in shown if r["source_key"] == "ACCOUNT_PROFILE")
    rank = [r["source_key"] for r in shown if r["eligible"]].index("ACCOUNT_PROFILE") + 1

    fetched = _fetch(client, case, "ACCOUNT_PROFILE")
    assert fetched.status_code == 200, fetched.text
    record_id = fetched.json()["evidence_records"][0]["id"]
    assert _fetch(client, case, "ACCOUNT_PROFILE").status_code == 200  # idempotent: no 2nd event

    current = get_case(env.sessions(), case.id)
    assert current is not None
    recommended = fetched.json()["recommended_decision"]
    body = {
        "decision": recommended,
        "typology": "APP_DIGITAL_ARREST" if recommended == "CONFIRM_FRAUD" else None,
        "confidence": 4,
    }
    disposed = _dispose(client, current, body)
    assert disposed.status_code == 200, disposed.text

    traces = _traces(env, case)
    assert [t["type"] for t in traces] == ["CASE_OPENED", "EVIDENCE_REQUESTED", "DISPOSITION"]
    opened, requested, disposition = traces
    assert opened["user_id"] is None  # the engine opened it
    assert opened["payload"] == {"protocol": "COERCION", "decision": "PROTECTIVE_HOLD"}
    assert requested["user_id"] == principal.user_id
    assert requested["payload"] == {
        "source_key": "ACCOUNT_PROFILE",
        "evidence_record_id": record_id,
        "rank_shown": rank,
        "net_value_shown_paise": row["net_value_paise"],
        "settle_pct_shown": row["settle_pct"],
        "refresh": False,
    }
    assert disposition["user_id"] == principal.user_id
    assert disposition["payload"] == {
        "decision": recommended,
        "typology": body["typology"],
        "confidence": 4,
        "is_override": False,
        "seconds_since_opened": 0.0,  # the fixed clock has not moved
    }


def test_the_console_sends_view_events_in_a_batch(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, principal = writer
    case = _held_case(env)
    record_id = _fetch(client, case, "ACCOUNT_PROFILE").json()["evidence_records"][0]["id"]
    at = T0.isoformat()
    response = _post(
        client,
        case.id,
        [
            {"type": "CASE_VIEWED", "at": at},
            {"type": "EVIDENCE_VIEWED", "at": at, "payload": {"evidence_record_id": record_id}},
            {"type": "NARRATIVE_EXPANDED", "at": at, "payload": {"evidence_record_id": record_id}},
            {"type": "WCMM_VIEWED", "at": (T0 + timedelta(days=1)).isoformat()},  # clock ahead
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"recorded": 4}
    views = [
        t for t in _traces(env, case) if t["type"] not in {"CASE_OPENED", "EVIDENCE_REQUESTED"}
    ]
    assert [t["type"] for t in views] == [
        "CASE_VIEWED",
        "EVIDENCE_VIEWED",
        "NARRATIVE_EXPANDED",
        "WCMM_VIEWED",
    ]
    assert all(t["user_id"] == principal.user_id for t in views)
    assert views[1]["payload"] == {"evidence_record_id": record_id}
    assert views[0]["payload"] == {}
    assert views[3]["at"] == T0  # a browser time after now is kept at now


def _count(e: Env) -> int:
    with e.owner.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(trace_events)).scalar_one())


def test_a_batch_with_any_bad_event_is_refused_whole(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    case = _held_case(env)
    other = _held_case(env, "HOLD_FOR_REVIEW")
    foreign = _fetch(client, other, "ACCOUNT_PROFILE").json()["evidence_records"][0]["id"]
    before = _count(env)
    at = T0.isoformat()
    ok = {"type": "CASE_VIEWED", "at": at}
    for bad, detail in [
        ({"type": "DISPOSITION", "at": at}, "recorded by the server"),  # an action, not a view
        (
            {"type": "EVIDENCE_VIEWED", "at": at, "payload": {"evidence_record_id": foreign}},
            "evidence record of this case",
        ),
        ({"type": "EVIDENCE_VIEWED", "at": at}, "evidence record of this case"),
        (
            {"type": "WCMM_VIEWED", "at": at, "payload": {"evidence_record_id": foreign}},
            "names no evidence record",
        ),
    ]:
        response = _post(client, case.id, [ok, bad])
        assert response.status_code == 422, (bad, response.text)
        assert detail in response.json()["detail"]
    # The schema refuses unknown types, extra payload fields and an empty batch.
    for events in (
        [{"type": "SOMETHING", "at": at}],
        [{"type": "CASE_VIEWED", "at": at, "payload": {"note": "free text"}}],
        [],
    ):
        assert _post(client, case.id, events).status_code == 422
    assert _post(client, uuid4(), [ok]).status_code == 404
    assert _count(env) == before
