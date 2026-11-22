"""`GET /metrics/summary` (docs/22 §7, docs/20 §4, #126) on a real database: the Overview's live
KPIs over the bank's own data, and its alert banner."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.navigator.api import open_case
from delphi.platform.db import migration_head
from delphi.platform.jobs import JobType, enqueue
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform

from ._db import MIGRATIONS_DIR
from .test_guardian_store import T0, Env, env  # noqa: F401 - the env fixture

VIEWER = Principal(UUID(int=11), frozenset({Permission.VIEW}))
NOW = T0 + timedelta(hours=2)
POSTERIOR = {"GENUINE": 0.2, "APP_DIGITAL_ARREST": 0.7, "ATO": 0.05, "UNEXPLAINED": 0.05}


@pytest.fixture
def client(env: Env) -> Iterator[TestClient]:  # noqa: F811
    env.clock.advance(NOW - env.clock.now())
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


def _decide(
    e: Env, session_id: UUID, decision: str, *, seconds: int, dd: bool, at: timedelta
) -> UUID:
    with e.owner.begin() as conn:
        return conn.execute(
            text(
                "UPDATE delphi.guardian_sessions SET status = 'DECIDED', decision = :d,"
                " terminal_reason = 'STOP', started_at = :s, decided_at = :t,"
                " dd_pack_complete = :dd"
                " WHERE id = :id RETURNING payment_id"
            ),
            {
                "d": decision,
                "s": T0 + at,
                "t": T0 + at + timedelta(seconds=seconds),
                "dd": dd,
                "id": session_id,
            },
        ).scalar_one()


def _probe(e: Env, session_id: UUID, key: str, mode: str, bits: float | None, seq: int) -> None:
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delphi.probe_runs (id, session_id, seq, probe_key, mode, status,"
                " info_yield_bits, outcome, started_at, completed_at)"
                " VALUES (:id, :s, :q, :k, :m, 'COMPLETED', :b, 'OBSERVED', :t, :t)"
            ),
            {"id": uuid4(), "s": session_id, "q": seq, "k": key, "m": mode, "b": bits, "t": T0},
        )


def _label(e: Env, payment_id: UUID, known_at: timedelta) -> None:
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delphi.labels (id, subject_type, subject_id, hypothesis_key,"
                " label_source, known_at) VALUES (:id, 'PAYMENT', :p, 'APP_DIGITAL_ARREST',"
                " 'OUTREACH_CONFIRMED', :k)"
            ),
            {"id": uuid4(), "p": payment_id, "k": T0 + known_at},
        )


def test_the_summary_counts_the_live_sessions_cases_and_labels(
    env: Env,  # noqa: F811
    client: TestClient,
) -> None:
    # Three decided sessions: a release (60 s), and two holds (120 s, 300 s), one labelled.
    release = env.new_session()
    _decide(env, release, "RELEASE", seconds=60, dd=True, at=timedelta(minutes=1))
    _probe(env, release, "CALL_STATUS", "PASSIVE", 0.4, 1)
    held = env.new_session()
    held_payment = _decide(
        env, held, "PROTECTIVE_HOLD", seconds=120, dd=True, at=timedelta(minutes=5)
    )
    _probe(env, held, "CALL_STATUS", "PASSIVE", 0.8, 1)
    _probe(env, held, "SPELL_BREAKER_DIGITAL_ARREST", "ACTIVE", 1.2, 2)
    _label(env, held_payment, timedelta(days=1))
    other = env.new_session()
    _decide(env, other, "HOLD_FOR_REVIEW", seconds=300, dd=False, at=timedelta(minutes=9))
    _probe(env, other, "OTP_STEP_UP", "ACTIVE", None, 1)  # no yield yet: not averaged
    env.new_session()  # still running: not counted
    # One open case, past its SLA.
    with env.sessions.begin() as s:
        open_case(
            s,
            env.clock,
            decision="PROTECTIVE_HOLD",
            payment_id=held_payment,
            guardian_session_id=held,
            kb_version_id=env.kb_version_id,
            posterior=POSTERIOR,
            prior_components={},
            amount_at_stake_paise=10_000_00,
            unexplained=False,
        )
    with env.owner.begin() as conn:
        conn.execute(
            text("UPDATE delphi.cases SET sla_due_at = :t"), {"t": NOW - timedelta(minutes=1)}
        )

    body = client.get("/api/v1/metrics/summary").json()
    g = body["guardian"]
    assert g["decided"] == 3
    assert g["decisions"] == {"HOLD_FOR_REVIEW": 1, "PROTECTIVE_HOLD": 1, "RELEASE": 1}
    assert g["median_session_seconds"] == pytest.approx(120.0)
    assert g["active_probes_per_session"] == pytest.approx(2 / 3)
    assert g["info_yield_bits"] == pytest.approx(
        {"CALL_STATUS": 0.6, "SPELL_BREAKER_DIGITAL_ARREST": 1.2}
    )
    assert g["held"] == 2 and g["label_coverage_held"] == pytest.approx(0.5)
    assert g["dd_pack_completeness"] == pytest.approx(2 / 3)
    c = body["cases"]
    assert c["open_cases"] == 1 and c["open_by_protocol"] == {"COERCION": 1}
    assert c["sla_breaches"] == 1
    assert c["disposed"] == 0 and c["median_hold_resolution_minutes"] is None
    assert body["golden_hour_ratio"] is None and body["window_hours"] == 24
    assert body["alerts"] == []
    assert set(body["alerts_not_tracked"]) == {
        "BACKUP_AGE",
        "SSE_OUTBOX_LAG",
        "PROBE_YIELD_DEGRADED",
    }


def test_an_empty_window_is_undefined_not_zero(client: TestClient) -> None:
    g = client.get("/api/v1/metrics/summary", params={"hours": 1}).json()["guardian"]
    assert g["decided"] == 0 and g["decisions"] == {}
    for figure in ("median_session_seconds", "active_probes_per_session", "label_coverage_held"):
        assert g[figure] is None


def test_the_banner_raises_failed_and_stuck_jobs(env: Env, client: TestClient) -> None:  # noqa: F811
    with env.sessions.begin() as s:
        enqueue(s, env.clock, JobType.TRAIN_SENSING, idempotency_key="stuck")
        enqueue(s, env.clock, JobType.TRAIN_SENSING, idempotency_key="failed")
    with env.owner.begin() as conn:
        conn.execute(
            text("UPDATE delphi.jobs SET run_after = :t WHERE idempotency_key = 'stuck'"),
            {"t": NOW - timedelta(minutes=11)},
        )
        conn.execute(
            text(
                "UPDATE delphi.jobs SET status = 'FAILED', finished_at = :t"
                " WHERE idempotency_key = 'failed'"
            ),
            {"t": NOW - timedelta(minutes=5)},
        )
    alerts = {a["code"]: a["count"] for a in client.get("/api/v1/metrics/summary").json()["alerts"]}
    assert alerts == {"JOB_FAILURES": 1, "JOBS_QUEUED": 1}


def test_the_banner_raises_llm_fallbacks_only_when_a_model_is_in_use(
    env: Env,  # noqa: F811
    client: TestClient,
) -> None:
    def calls(provider: str, n: int, offset: int) -> None:
        with env.owner.begin() as conn:
            for i in range(n):
                conn.execute(
                    text(
                        "INSERT INTO delphi.llm_calls (id, purpose, provider, prompt_version,"
                        " status, validation, input_tokens, output_tokens, latency_ms, created_at)"
                        " VALUES (:id, 'EVIDENCE_NARRATIVE', :p, 'v1', 'OK', '{}', 0, 0, 1, :t)"
                    ),
                    {"id": uuid4(), "p": provider, "t": NOW - timedelta(minutes=offset + i)},
                )

    calls("template", 20, 0)  # template mode only: no model, so no fallback alert
    assert client.get("/api/v1/metrics/summary").json()["alerts"] == []
    calls("anthropic", 8, 0)  # the 20 newest are now 8 model calls and 12 template ones
    [alert] = client.get("/api/v1/metrics/summary").json()["alerts"]
    assert alert["code"] == "LLM_FALLBACK" and alert["count"] == 12
