"""The Guardian read API on a real database (docs/16 "Guardian", docs/08 §6, docs/09 §7, #98):
sessions decided by the real service with fixture priors, then listed, read, and packed."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.guardian.service import Decided
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
