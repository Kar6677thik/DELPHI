"""The Guardian read API on a real database (docs/16 "Guardian", docs/08 §6, docs/09 §7, #98):
sessions decided by the real service with fixture priors, then listed, read, and packed."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.engine import SessionFacts
from delphi.guardian import ddpack
from delphi.guardian.service import Decided, GuardianService
from delphi.guardian.store import Runner, SessionMode
from delphi.platform.db import migration_head
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform

from ._db import MIGRATIONS_DIR
from .test_guardian_service import (
    CLEAN,
    DIGITAL_ARREST,
    GENUINE,
    GENUINE_SURE,
    SCAM,
    T0,
    Env,
    FixedPrior,
    _run,
    make_env,
)

VIEWER = Principal(UUID(int=12), frozenset({Permission.VIEW}))
API = "/api/v1/guardian/sessions"


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    yield from make_env(create_database)


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


def _decide(env: Env, prior: dict[str, float], outcomes: dict[str, str], amount_inr: int) -> UUID:
    result = _run(
        env, env.service(FixedPrior(prior, p_mule=None), outcomes), env.payment(amount_inr)
    )
    assert isinstance(result, Decided)
    return result.session.id


def _three(env: Env) -> tuple[UUID, UUID, UUID]:
    """A fast-path release, a release on STOP and a protective hold, a minute apart."""
    fast = _decide(env, GENUINE_SURE, CLEAN, 2_000)
    env.clock.advance(timedelta(seconds=60))
    stop = _decide(env, GENUINE, CLEAN, 480_000)
    env.clock.advance(timedelta(seconds=60))
    hold = _decide(env, DIGITAL_ARREST, SCAM, 480_000)
    return fast, stop, hold


def test_the_list_is_newest_first_paged_and_filtered(env: Env, client: TestClient) -> None:
    fast, stop, hold = _three(env)
    first = client.get(API, params={"limit": 2}).json()
    assert [i["id"] for i in first["items"]] == [str(hold), str(stop)]
    rest = client.get(API, params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [i["id"] for i in rest["items"]] == [str(fast)] and rest["next_cursor"] is None
    held = client.get(API, params={"decision": "PROTECTIVE_HOLD"}).json()["items"]
    assert [i["id"] for i in held] == [str(hold)]
    assert held[0]["leading_hypothesis"] == "APP_DIGITAL_ARREST" and held[0]["case_id"]
    window = {
        "from": (T0.replace(second=30)).isoformat(),
        "to": T0.replace(minute=1, second=30).isoformat(),
    }
    assert [i["id"] for i in client.get(API, params=window).json()["items"]] == [str(stop)]
    assert client.get(API, params={"unexplained": "true"}).json()["items"] == []
    assert len(client.get(API, params={"status": "DECIDED"}).json()["items"]) == 3


@pytest.mark.parametrize(
    "params",
    [{"cursor": "not-a-cursor"}, {"from": "2026-11-15T10:00:00"}, {"decision": "MAYBE"}],
)
def test_bad_queries_are_validation_failures(client: TestClient, params: dict[str, str]) -> None:
    response = client.get(API, params=params)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_one_session_carries_its_prior_probe_runs_baselines_and_etag(
    env: Env, client: TestClient
) -> None:
    _, _, hold = _three(env)
    response = client.get(f"{API}/{hold}")
    assert response.status_code == 200
    body = response.json()
    assert response.headers["etag"] == f'"{body["version"]}"'
    assert body["decision"] == "PROTECTIVE_HOLD" and body["terminal_reason"] == "STOP"
    assert body["prior"]["top_features"][0]["feature"] == "amount_log10"
    assert set(body["risk_by_decision"]) == {
        "RELEASE",
        "HOLD_FOR_REVIEW",
        "PROTECTIVE_HOLD",
        "DECLINE",
    }
    assert all(isinstance(v, int) for v in body["risk_by_decision"].values())
    seqs = [r["seq"] for r in body["probe_runs"]]
    assert seqs == sorted(seqs) and seqs
    assert all(r["mode"] == "PASSIVE" for r in body["probe_runs"])
    # ₹4,80,000 to a payee saved minutes ago (never, here): B1 holds; B2's r = 1 − P(GENUINE)
    # is far above 0.60 under a digital-arrest prior.
    assert body["baselines"] == {"B1": "HOLD_FOR_REVIEW", "B2": "HOLD_FOR_REVIEW"}
    assert body["dd_pack_complete"] is True
    assert client.get(f"{API}/{uuid4()}").status_code == 404


def test_baselines_follow_b1_and_b2(env: Env, client: TestClient) -> None:
    fast, stop, _ = _three(env)
    # ₹2,000: under every B1 threshold; P(GENUINE) is near 1, so B2 releases too.
    assert client.get(f"{API}/{fast}").json()["baselines"] == {"B1": "RELEASE", "B2": "RELEASE"}
    assert client.get(f"{API}/{stop}").json()["baselines"]["B1"] == "HOLD_FOR_REVIEW"
    # A payee the registry listed before the payment: B1 declines (docs/22 §2).
    payment = env.payment(1_000)
    with env.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bank.sim_registry_listings (account_id, listed_at)"
                " SELECT payee_account_id, :t FROM bank.payments WHERE id = :p"
            ),
            {"t": T0.replace(hour=9), "p": payment},
        )
    record = env.service(FixedPrior(GENUINE_SURE, p_mule=None), CLEAN).start(
        payment, mode=SessionMode.LIVE, runner=Runner.API
    )
    assert client.get(f"{API}/{record.id}").json()["baselines"]["B1"] == "DECLINE"


def _sections(pack: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {s["number"]: s for s in pack["sections"]}


def test_a_fast_path_pack_is_complete_with_no_observations(env: Env, client: TestClient) -> None:
    fast, _, _ = _three(env)
    pack = client.get(f"{API}/{fast}/dd-pack").json()
    assert pack["complete"] is True
    sections = _sections(pack)
    assert sections[3]["content"] == "Not applicable" and not sections[3]["required"]
    assert sections[3]["note"] == "Fast path: no observations."
    assert sections[8]["content"]["ok"] is True and sections[8]["content"]["entries_checked"] >= 2
    assert "KB version" in sections[9]["content"]
    assert sections[1]["content"]["customer"].startswith("C-")  # a pseudonym, never a name
    assert "CUST" not in str(pack)


def test_a_full_session_pack_has_its_timeline_and_guardrails(env: Env, client: TestClient) -> None:
    _, _, hold = _three(env)
    pack = client.get(f"{API}/{hold}/dd-pack").json()
    assert pack["complete"] is True
    sections = _sections(pack)
    timeline = sections[3]["content"]
    assert timeline and all(step["posterior_after"] for step in timeline)
    assert sections[4]["content"]  # a list of ineligible sources, or "None applied."
    engine = sections[5]["content"]["engine"]
    assert engine["decision"] == "PROTECTIVE_HOLD" and engine["baselines"]["B1"]
    page = client.get(f"{API}/{hold}/dd-pack", params={"format": "html"})
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "<script" not in page.text and "9. Method note" in page.text


def test_an_undecided_session_or_a_broken_audit_chain_is_not_complete(
    env: Env, client: TestClient
) -> None:
    record = env.service(FixedPrior(GENUINE), CLEAN).start(
        env.payment(480_000), mode=SessionMode.LIVE, runner=Runner.API
    )
    pack = client.get(f"{API}/{record.id}/dd-pack").json()
    assert pack["complete"] is False and _sections(pack)[5]["content"] is None
    assert client.get(f"{API}/{record.id}").json()["dd_pack_complete"] is False
    fast, _, _ = _three(env)
    with env.owner.begin() as conn:  # the owner role can; the app roles cannot (docs/17 §2)
        conn.execute(
            text(
                "UPDATE delphi.audit_log SET payload = '{}'::jsonb"
                " WHERE entity_id = :s AND action = 'guardian.session.decided'"
            ),
            {"s": str(fast)},
        )
    broken = client.get(f"{API}/{fast}/dd-pack").json()
    assert broken["complete"] is False
    assert "broken" in _sections(broken)[8]["note"]
    assert client.get(f"{API}/{uuid4()}/dd-pack").status_code == 404


def test_a_stop_with_no_eligible_source_lists_the_stop_steps_guardrails(
    env: Env, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last planning step has no probe run: its ineligible sources come from the decided
    audit entry's final candidates (docs/09 §7 sections 3 and 4)."""
    facts = GuardianService._facts

    def capped(self: GuardianService, s: Any, ctx: Any) -> SessionFacts:
        return replace(facts(self, s, ctx), fairness_cap_reached=True)

    monkeypatch.setattr(GuardianService, "_facts", capped)
    session_id = _decide(env, DIGITAL_ARREST, SCAM, 480_000)
    detail = client.get(f"{API}/{session_id}").json()
    assert detail["terminal_reason"] == "BUDGET_EXHAUSTED"  # docs/06 §8: NO_ELIGIBLE_SOURCES
    pack = client.get(f"{API}/{session_id}/dd-pack").json()
    guardrails = _sections(pack)[4]["content"]
    assert isinstance(guardrails, list)
    assert {g["reason"] for g in guardrails} >= {"FAIRNESS_CAP"}
    stop = _sections(pack)[3]["content"][-1]
    assert stop["status"] == "STOPPED" and stop["outcome"] == "BUDGET_EXHAUSTED"
    assert stop["alternatives"] and not any(a["eligible"] for a in stop["alternatives"])
    assert pack["complete"] is True


def test_a_failing_pack_build_never_blocks_the_decision(
    env: Env, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any) -> Any:
        raise RuntimeError("the pack could not be built")

    monkeypatch.setattr(ddpack, "build", broken)
    session_id = _decide(env, DIGITAL_ARREST, SCAM, 480_000)
    detail = client.get(f"{API}/{session_id}").json()
    assert detail["status"] == "DECIDED" and detail["decision"] == "PROTECTIVE_HOLD"
    assert detail["dd_pack_complete"] is False


def _cursor(raw: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "raw",
    [
        {"at": "2026-01-01T00:00:00+00:00", "id": 5},  # an id that is not a string
        ["2026-01-01T00:00:00+00:00"],  # not an object
        {"at": 5, "id": "x"},
    ],
)
def test_a_malformed_cursor_is_a_validation_failure(client: TestClient, raw: object) -> None:
    response = client.get(API, params={"cursor": _cursor(raw)})
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_FAILED"


def test_paging_breaks_ties_on_id_and_keeps_the_filters(env: Env, client: TestClient) -> None:
    """Sessions started at the same instant page by id; a cursor works together with filters."""
    ids = [_decide(env, GENUINE_SURE, CLEAN, 2_000) for _ in range(3)]  # same clock: same start
    _decide(env, DIGITAL_ARREST, SCAM, 480_000)  # a hold, filtered out below
    first = client.get(API, params={"limit": 2, "decision": "RELEASE"}).json()
    rest = client.get(
        API, params={"limit": 2, "decision": "RELEASE", "cursor": first["next_cursor"]}
    ).json()
    paged = [i["id"] for i in first["items"]] + [i["id"] for i in rest["items"]]
    assert paged == sorted((str(i) for i in ids), reverse=True)  # newest id first within a tie
    assert rest["next_cursor"] is None


def test_decision_and_mode_filters_follow_the_kb_and_the_session_modes(
    env: Env, client: TestClient
) -> None:
    _three(env)
    assert client.get(API, params={"decision": "SEND_MONEY"}).status_code == 422  # not in the KB
    assert len(client.get(API, params={"mode": "LIVE"}).json()["items"]) == 3
    assert client.get(API, params={"mode": "EVAL"}).json()["items"] == []
