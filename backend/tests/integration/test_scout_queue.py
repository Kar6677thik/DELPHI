"""The unexplained queue's rules on a real database (docs/11 §1, FR-SCT-01, #117): decided
sessions flagged unexplained and disposed account reviews at the threshold enter; dismissed
items and members of a promoted typology's cluster leave."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text, update

from delphi.api.security import Permission, Principal, current_principal
from delphi.guardian.tables import guardian_sessions
from delphi.kb.store import active_version
from delphi.scout.api import queue_items
from delphi.scout.tables import scout_clusters, scout_runs, typology_drafts

from .test_cases import BROWSER, T0, Env, _investigator, env, writer  # noqa: F401 - fixtures

FDE = Principal(UUID(int=31), frozenset(Permission))


def _decided(e: Env, *, unexplained: bool, minutes: int = 0) -> UUID:
    session_id = e.new_session()
    with e.owner.begin() as conn:
        conn.execute(
            update(guardian_sessions)
            .where(guardian_sessions.c.id == session_id)
            .values(
                status="DECIDED",
                decision="HOLD_FOR_REVIEW",
                terminal_reason="STOP",
                decided_at=T0 + timedelta(minutes=minutes),
                unexplained=unexplained,
                posterior={"GENUINE": 0.5, "UNEXPLAINED": 0.5 if unexplained else 0.1},
            )
        )
    return session_id


def _review(e: Env, p_unexplained: float) -> UUID:
    """A disposed ACCOUNT_REVIEW case with this terminal P(UNEXPLAINED)."""
    case_id, account = uuid4(), uuid4()
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bank.accounts (id, customer_id, bank_code, opened_at, kyc_level,"
                " status, balance_paise) VALUES (:a, NULL, 'DLPH', :t, 'FULL', 'ACTIVE', 0)"
            ),
            {"a": account, "t": T0},
        )
        conn.execute(
            text(
                "INSERT INTO delphi.cases (id, type, protocol, status, subject_account_id,"
                " kb_version_id, prior_components, posterior, leading_hypothesis,"
                " amount_at_stake_paise, opened_at, sla_due_at, disposition, disposed_at,"
                " is_override, dd_pack_complete, unexplained, version)"
                " VALUES (:c, 'ACCOUNT_REVIEW', 'STANDARD', 'DISPOSED', :a, :k, '{}',"
                " CAST(:p AS jsonb), 'UNEXPLAINED', 0, :t, :t, 'CLEAR', :t, false, false,"
                " false, 2)"
            ),
            {
                "c": case_id,
                "a": account,
                "k": e.kb_version_id,
                "p": json.dumps({"MULE": 1 - p_unexplained, "UNEXPLAINED": p_unexplained}),
                "t": T0,
            },
        )
    return case_id


def _ids(e: Env) -> list[UUID]:
    with e.sessions() as s:
        version = active_version(s)
        assert version is not None
        return [i.subject_id for i in queue_items(s, version.kb)]


def test_the_entry_rules(env: Env) -> None:  # noqa: F811
    flagged = _decided(env, unexplained=True, minutes=1)
    _decided(env, unexplained=False)
    env.new_session()  # not decided yet
    review = _review(env, 0.4)
    _review(env, 0.2)  # below unexplained_routing_threshold (0.35)
    assert _ids(env) == [review, flagged]  # oldest first


def test_a_promoted_typology_takes_its_members_out(env: Env) -> None:  # noqa: F811
    member, other = _decided(env, unexplained=True), _decided(env, unexplained=True)
    run, cluster, draft = uuid4(), uuid4(), uuid4()
    ref = {"subject_type": "GUARDIAN_SESSION", "subject_id": str(member)}
    with env.owner.begin() as conn:
        conn.execute(
            scout_runs.insert().values(id=run, started_at=T0, n_items=2, n_clusters=1, params={})
        )
        conn.execute(
            scout_clusters.insert().values(
                id=cluster,
                run_id=run,
                size=1,
                member_refs=[ref],
                distinguishing_features=[],
                outcome_profile={},
                created_at=T0,
            )
        )
        conn.execute(
            typology_drafts.insert().values(
                id=draft,
                cluster_id=cluster,
                status="CANDIDATE",
                revision=1,
                spec={},
                editors=[],
                created_at=T0,
                updated_at=T0,
                version=1,
            )
        )
    assert set(_ids(env)) == {member, other}  # a candidate assigns nothing yet
    with env.owner.begin() as conn:
        conn.execute(
            update(typology_drafts).where(typology_drafts.c.id == draft).values(status="PROMOTED")
        )
    assert _ids(env) == [other]


def _post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    headers = {"Idempotency-Key": str(uuid4()), **BROWSER}
    return client.post(f"/api/v1{path}", json=body, headers=headers)


def test_an_fde_engineer_dismisses_an_item(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, investigator = writer
    flagged = _decided(env, unexplained=True)
    listed = client.get("/api/v1/scout/queue").json()["items"]
    assert [i["id"] for i in listed] == [str(flagged)]
    assert listed[0]["subject_type"] == "GUARDIAN_SESSION"
    path = f"/scout/queue/{flagged}/dismiss"
    assert _post(client, path, {"reason": "Known merchant refund"}).status_code == 403
    fde = _investigator(env)  # a real user row for the dismissal's foreign key
    client.app.dependency_overrides[current_principal] = lambda: Principal(  # type: ignore[attr-defined]
        fde.user_id, frozenset(Permission)
    )
    assert _post(client, path, {"reason": " "}).status_code == 422
    dismissed = _post(client, path, {"reason": "Known merchant refund"})
    assert dismissed.status_code == 200, dismissed.text
    assert client.get("/api/v1/scout/queue").json()["items"] == []
    assert _post(client, path, {"reason": "again"}).status_code == 404
    with env.owner.connect() as conn:
        audited = conn.execute(
            text("SELECT count(*) FROM delphi.audit_log WHERE action = 'scout.queue.dismissed'")
        ).scalar_one()
    assert audited == 1
    del investigator
