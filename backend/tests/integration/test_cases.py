"""The case stub on a real database (docs/05 §4, docs/09 §1, docs/16 "Cases", #97): a Guardian
hold opens a PAYMENT_HOLD case with the session's posterior and the right protocol and SLA, one
open case per payment, and the read-only API pages the queue."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table, func, inspect, select, update
from sqlalchemy.exc import IntegrityError

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.guardian.tables import guardian_sessions
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.kb.store import ensure_kb_v1
from delphi.navigator.api import (
    CaseExistsError,
    CaseProtocol,
    CaseRecord,
    CaseSort,
    CaseStatus,
    CaseType,
    get_case,
    list_cases,
    open_case,
)
from delphi.navigator.tables import cases, metadata
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.errors import DelphiError
from delphi.platform.settings import Settings
from delphi.platform.tables import audit_log, outbox_events
from delphi.platform.wiring import Platform
from delphi.probes.registry import CLASSIFIER_NAMES

from ._db import MIGRATIONS_DIR, engine_as, upgrade
from .test_guardian_store import KB_DIR, T0, Env

POSTERIOR = {
    "GENUINE": 0.08,
    "ATO": 0.02,
    "APP_DIGITAL_ARREST": 0.71,
    "APP_INVESTMENT": 0.04,
    "MULE": 0.05,
    "UNEXPLAINED": 0.10,
}
PRIOR = {"p_model": {"GENUINE": 0.9, "APP_DIGITAL_ARREST": 0.1}, "q": 0.93, "open_mass": 0.05}
AMOUNT = 48_000_000
VIEWER = Principal(UUID(int=11), frozenset({Permission.VIEW}))


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    url = create_database()
    upgrade(url)
    owner = engine_as(url)
    app = engine_as(url, "delphi_app")
    clock = FixedClock(T0)
    with make_session_factory(owner).begin() as s:
        kb_version_id = ensure_kb_v1(s, clock, load_and_validate(KB_DIR, CLASSIFIER_NAMES))
    assert kb_version_id is not None
    yield Env(owner, make_session_factory(app), clock, kb_version_id)
    app.dispose()
    owner.dispose()


@dataclass(frozen=True)
class Held:
    payment_id: UUID
    session_id: UUID


def _held(env: Env) -> Held:
    payment = env.payment()
    return Held(payment, env.new_session(payment))


def _open(env: Env, held: Held, decision: str, **kw: object) -> CaseRecord:
    with env.sessions.begin() as s:
        return open_case(
            s,
            env.clock,
            decision=decision,
            payment_id=held.payment_id,
            guardian_session_id=held.session_id,
            kb_version_id=env.kb_version_id,
            prior_components=PRIOR,
            amount_at_stake_paise=AMOUNT,
            unexplained=False,
            **{"posterior": POSTERIOR, **kw},  # type: ignore[arg-type]
        )


def test_the_table_description_matches_the_migrated_schema(env: Env) -> None:
    insp = inspect(env.owner)
    for table in metadata.tables.values():
        reflected = [c["name"] for c in insp.get_columns(table.name, schema="delphi")]
        assert reflected == [c.name for c in table.columns], table.name


def test_a_protective_hold_opens_a_coercion_case_with_the_sessions_posterior(env: Env) -> None:
    """FR-GDN-08 / FR-NAV-01: PROTECTIVE_HOLD → PAYMENT_HOLD, COERCION, 30-minute SLA."""
    case = _open(env, _held(env), "PROTECTIVE_HOLD")
    assert (case.type, case.protocol, case.status) == (
        CaseType.PAYMENT_HOLD,
        CaseProtocol.COERCION,
        CaseStatus.OPEN,
    )
    assert case.posterior == POSTERIOR and case.prior_components == PRIOR
    assert case.leading_hypothesis == "APP_DIGITAL_ARREST"
    assert case.opened_at == T0 and case.sla_due_at == T0 + timedelta(minutes=30)
    assert case.kb_version_id == env.kb_version_id and case.amount_at_stake_paise == AMOUNT
    assert case.recommended_decision is None and case.risk_by_decision is None  # #103
    assert case.version == 1


def test_a_hold_for_review_is_standard_and_demo_time_scales_the_sla(
    env: Env, kb: KnowledgeBase
) -> None:
    case = _open(env, _held(env), "HOLD_FOR_REVIEW")
    assert case.protocol is CaseProtocol.STANDARD
    assert case.sla_due_at == T0 + timedelta(minutes=240)
    demo = _open(env, _held(env), "HOLD_FOR_REVIEW", demo_time=True)  # the pinned KB's 1/30
    scale = kb.config.time.demo_time_scale
    assert demo.sla_due_at == T0 + timedelta(minutes=240 * scale)


def test_only_a_hold_opens_a_case(env: Env) -> None:
    with pytest.raises(DelphiError, match="opens no case"):
        _open(env, _held(env), "RELEASE")
    with pytest.raises(DelphiError, match="posterior"):
        _open(env, _held(env), "HOLD_FOR_REVIEW", posterior={})


def test_a_retried_open_returns_the_case_it_opened(env: Env) -> None:
    """#164 review: a retried Guardian transaction gets the same case back, written once."""
    held = _held(env)
    first = _open(env, held, "HOLD_FOR_REVIEW")
    assert _open(env, held, "HOLD_FOR_REVIEW") == first
    assert _count(env, audit_log) == 1 and _count(env, outbox_events) == 1
    with env.sessions() as s:
        assert get_case(s, first.id) == first


def test_a_payment_with_an_open_case_refuses_another_session(env: Env) -> None:
    held = _held(env)
    _open(env, held, "HOLD_FOR_REVIEW")
    with pytest.raises(CaseExistsError):
        _open(env, Held(held.payment_id, uuid4()), "PROTECTIVE_HOLD")


def _count(env: Env, table: Table) -> int:
    """Rows about cases only: loading KB v1 writes its own audit row."""
    column = table.c.action if table is audit_log else table.c.type
    query = select(func.count()).select_from(table).where(column == "case.opened")
    with env.owner.connect() as conn:
        return int(conn.execute(query).scalar_one())


def test_opening_audits_and_publishes_in_the_same_transaction(env: Env) -> None:
    """FR-PLT-04, docs/16 §3: case.opened is audited and announced with the case, and a
    rollback leaves neither."""
    case = _open(env, _held(env), "PROTECTIVE_HOLD")
    with env.owner.connect() as conn:
        audit = (
            conn.execute(select(audit_log).where(audit_log.c.action == "case.opened"))
            .mappings()
            .one()
        )
        event = (
            conn.execute(select(outbox_events).where(outbox_events.c.type == "case.opened"))
            .mappings()
            .one()
        )
    assert (audit["action"], audit["entity_type"], audit["entity_id"]) == (
        "case.opened",
        "case",
        str(case.id),
    )
    assert audit["actor_type"] == "ENGINE" and audit["payload"]["protocol"] == "COERCION"
    assert (event["type"], event["entity_id"]) == ("case.opened", str(case.id))
    held = _held(env)
    with env.sessions() as s:  # opened, then rolled back
        open_case(
            s,
            env.clock,
            decision="HOLD_FOR_REVIEW",
            payment_id=held.payment_id,
            guardian_session_id=held.session_id,
            kb_version_id=env.kb_version_id,
            posterior=POSTERIOR,
            prior_components=PRIOR,
            amount_at_stake_paise=AMOUNT,
            unexplained=False,
        )
        s.rollback()
    assert _count(env, audit_log) == 1 and _count(env, outbox_events) == 1
    with env.owner.connect() as conn:
        assert conn.execute(select(func.count()).select_from(cases)).scalar_one() == 1


def test_a_case_names_the_session_of_its_own_payment(env: Env) -> None:
    """The composite foreign key: a session that held another payment is refused."""
    other = _held(env)
    with pytest.raises(IntegrityError):
        _open(env, Held(env.payment(), other.session_id), "HOLD_FOR_REVIEW")


def test_the_session_links_to_its_case_by_a_foreign_key(env: Env) -> None:
    held = _held(env)
    case = _open(env, held, "PROTECTIVE_HOLD")
    with env.sessions.begin() as s:  # what #94 does once it opens the case
        s.execute(
            update(guardian_sessions)
            .where(guardian_sessions.c.id == held.session_id)
            .values(case_id=case.id)
        )
    with pytest.raises(IntegrityError), env.sessions.begin() as s:
        s.execute(
            update(guardian_sessions)
            .where(guardian_sessions.c.id == held.session_id)
            .values(case_id=uuid4())
        )


@pytest.mark.parametrize(
    "values",
    [
        {"status": "DISPOSED"},  # no disposition
        {"status": "DISPOSED", "disposition": "CLEAR", "disposed_at": T0},  # a review's
        {"status": "DISPOSED", "disposition": "CONFIRM_FRAUD", "disposed_at": T0},  # no typology
        {"disposition_confidence": 6},  # 1–5
        {"is_override": True, "override_rationale": "too short"},  # under 20 characters
    ],
)
def test_the_case_invariants_are_constraints(env: Env, values: dict[str, object]) -> None:
    case = _open(env, _held(env), "HOLD_FOR_REVIEW")
    with pytest.raises(IntegrityError), env.owner.begin() as conn:
        conn.execute(update(cases).where(cases.c.id == case.id).values(**values))


def test_a_complete_disposition_is_accepted(env: Env) -> None:
    case = _open(env, _held(env), "HOLD_FOR_REVIEW")
    with env.owner.begin() as conn:
        conn.execute(
            update(cases)
            .where(cases.c.id == case.id)
            .values(
                status="DISPOSED",
                disposition="CONFIRM_FRAUD",
                disposition_typology="APP_DIGITAL_ARREST",
                disposition_confidence=4,
                disposed_at=T0,
                is_override=True,
                override_rationale="The customer confirmed the call on a recorded line.",
            )
        )


def _queue(env: Env) -> list[CaseRecord]:
    """Three cases opened a minute apart: two COERCION (30 min SLA), one STANDARD (4 h)."""
    opened = []
    for decision in ("PROTECTIVE_HOLD", "HOLD_FOR_REVIEW", "PROTECTIVE_HOLD"):
        opened.append(_open(env, _held(env), decision))
        env.clock.advance(timedelta(minutes=1))
    return opened


def test_the_queue_is_sorted_filtered_and_paged(env: Env) -> None:
    a, b, c = _queue(env)
    with env.sessions() as s:
        by_sla = list_cases(s, limit=10)
        assert [r.id for r in by_sla] == [a.id, c.id, b.id]  # soonest SLA first
        newest = list_cases(s, limit=10, sort=CaseSort.OPENED_AT)
        assert [r.id for r in newest] == [c.id, b.id, a.id]
        coercion = list_cases(s, limit=10, protocol=CaseProtocol.COERCION)
        assert {r.id for r in coercion} == {a.id, c.id}
        assert list_cases(s, limit=10, status=CaseStatus.DISPOSED) == []


@pytest.fixture
def client(env: Env) -> Iterator[TestClient]:
    platform = Platform(
        settings=Settings(database_url="postgresql://unused/db", migrations_dir=MIGRATIONS_DIR),
        engine=env.owner,
        sessions=env.sessions,
        clock=env.clock,
        started_at=T0,
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    app = create_app(platform)
    app.dependency_overrides[current_principal] = lambda: VIEWER
    yield TestClient(app)


def test_the_api_pages_the_queue(env: Env, client: TestClient) -> None:
    a, b, c = _queue(env)
    first = client.get("/api/v1/cases", params={"limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert [i["id"] for i in body["items"]] == [str(a.id), str(c.id)]
    rest = client.get("/api/v1/cases", params={"limit": 2, "cursor": body["next_cursor"]}).json()
    assert [i["id"] for i in rest["items"]] == [str(b.id)] and rest["next_cursor"] is None
    standard = client.get("/api/v1/cases", params={"protocol": "STANDARD"}).json()
    assert [i["id"] for i in standard["items"]] == [str(b.id)]
    wrong = client.get("/api/v1/cases", params={"cursor": body["next_cursor"], "sort": "opened_at"})
    assert wrong.status_code == 422 and wrong.json()["code"] == "VALIDATION_FAILED"
    raw = json.dumps({"s": "sla_due_at", "at": "2026-11-13T18:30:00", "id": str(a.id)})
    naive = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    assert client.get("/api/v1/cases", params={"cursor": naive}).status_code == 422


def test_the_api_reads_one_case_with_its_etag(env: Env, client: TestClient) -> None:
    case = _open(env, _held(env), "PROTECTIVE_HOLD")
    response = client.get(f"/api/v1/cases/{case.id}")
    assert response.status_code == 200 and response.headers["ETag"] == '"1"'
    view = response.json()
    assert view["protocol"] == "COERCION" and view["leading_hypothesis"] == "APP_DIGITAL_ARREST"
    assert view["leading_hypothesis_name"] == "Coerced victim — digital arrest"  # kb/hypotheses
    assert view["posterior"] == POSTERIOR and view["sla_breached"] is False
    env.clock.advance(timedelta(minutes=31))
    assert client.get(f"/api/v1/cases/{case.id}").json()["sla_breached"] is True
    missing = client.get(f"/api/v1/cases/{uuid4()}")
    assert missing.status_code == 404 and missing.json()["code"] == "NOT_FOUND"
