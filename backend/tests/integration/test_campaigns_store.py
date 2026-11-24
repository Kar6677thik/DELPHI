"""Campaign recompute and the campaign API on a real database (docs/05 §5, docs/09 §8, #107):
cases sharing infrastructure join one campaign through a chain, GENUINE cases stay out, ids are
stable across recomputes, a merge keeps the oldest id, and the API lists and shows them."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from delphi.navigator.campaign_store import (
    VULNERABLE_RANK_WEIGHT,
    CampaignRecomputeJob,
    Limits,
    run_batch,
)
from delphi.navigator.tables import campaigns, cases

from .test_cases import POSTERIOR, Env, _held, _open, client, env  # noqa: F401 - fixtures
from .test_guardian_store import T0

WINDOW = 7
LIMITS = Limits(batch=50, batches_per_run=20, max_component=2_000)
GENUINE_POSTERIOR = {**POSTERIOR, "GENUINE": 0.9, "APP_DIGITAL_ARREST": 0.01}


class _Ctx:
    def report(self, fraction: float) -> bool:
        return True


def _case(e: Env, *, posterior: dict[str, float] = POSTERIOR, minutes: int = 0) -> UUID:
    e.clock.advance(timedelta(minutes=minutes))
    return _open(e, _held(e), "HOLD_FOR_REVIEW", posterior=posterior).id


def _payee(e: Env, case_id: UUID, payee: UUID) -> None:
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "UPDATE bank.payments SET payee_account_id = :b WHERE id ="
                " (SELECT subject_payment_id FROM delphi.cases WHERE id = :c)"
            ),
            {"b": payee, "c": case_id},
        )


def _device(e: Env, case_id: UUID, fingerprint: str) -> None:
    with e.owner.begin() as conn:
        customer = conn.execute(
            text(
                "SELECT a.customer_id FROM delphi.cases k JOIN bank.payments p"
                " ON p.id = k.subject_payment_id JOIN bank.accounts a ON a.id = p.payer_account_id"
                " WHERE k.id = :c"
            ),
            {"c": case_id},
        ).scalar_one()
        device = uuid4()
        conn.execute(
            text(
                "INSERT INTO bank.devices (id, customer_id, fingerprint, os, first_seen_at)"
                " VALUES (:d, :c, :f, 'ANDROID', :t)"
            ),
            {"d": device, "c": customer, "f": fingerprint, "t": T0},
        )
        conn.execute(
            text(
                "UPDATE bank.payments SET device_id = :d WHERE id ="
                " (SELECT subject_payment_id FROM delphi.cases WHERE id = :c)"
            ),
            {"d": device, "c": case_id},
        )


def _new_account(e: Env) -> UUID:
    account = uuid4()
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bank.accounts (id, customer_id, bank_code, opened_at, kyc_level,"
                " status, balance_paise) VALUES (:a, NULL, 'XBK1', :t, 'FULL', 'ACTIVE', 0)"
            ),
            {"a": account, "t": T0},
        )
    return account


def _recompute(e: Env, limits: Limits = LIMITS) -> dict[str, int]:
    """Run batches until nothing is touched; the counts summed."""
    totals: dict[str, int] = {}
    while True:
        with e.sessions.begin() as s:
            counts = run_batch(s, e.clock, window_days=WINDOW, limits=limits)
        if counts is None:
            return totals
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value


def _replay(e: Env) -> dict[str, int]:
    """Move the watermark back to the start, so every case is touched again."""
    with e.owner.begin() as conn:
        conn.execute(
            text(
                "UPDATE delphi.campaign_watermark SET at = '1970-01-01T00:00:00Z',"
                " case_id = '00000000-0000-0000-0000-000000000000'"
            )
        )
    return _recompute(e)


def _campaign_of(e: Env, case_id: UUID) -> UUID | None:
    with e.owner.connect() as conn:
        value: UUID | None = conn.execute(
            select(cases.c.campaign_id).where(cases.c.id == case_id)
        ).scalar_one()
        return value


def test_a_chain_of_shared_entities_is_one_campaign(env: Env) -> None:  # noqa: F811
    a, b, c = _case(env), _case(env, minutes=1), _case(env, minutes=2)
    genuine = _case(env, posterior=GENUINE_POSTERIOR, minutes=3)
    alone = _case(env, minutes=4)
    mule = _new_account(env)
    for case_id in (a, b, genuine):
        _payee(env, case_id, mule)  # a–b share the payee (so does the GENUINE case)
    _device(env, b, "fp-shared")
    _device(env, c, "fp-shared")  # b–c share a device: a, b and c are one campaign
    counts = _recompute(env)
    assert counts["new"] == 1 and counts["campaigns"] == 1 and counts["touched"] == 5
    campaign = _campaign_of(env, a)
    assert campaign is not None
    assert _campaign_of(env, b) == _campaign_of(env, c) == campaign
    assert _campaign_of(env, genuine) is None  # GENUINE is never in a campaign
    assert _campaign_of(env, alone) is None  # linked to nothing
    with env.owner.connect() as conn:
        row = conn.execute(select(campaigns).where(campaigns.c.id == campaign)).one()
        losses = conn.execute(
            select(cases.c.risk_by_decision).where(cases.c.id.in_((a, b, c)))
        ).scalars()
        expected = round(sum(r["RELEASE"] for r in losses))
    assert row.leading_hypothesis == "APP_DIGITAL_ARREST" and row.case_count == 3
    assert row.expected_loss_paise == expected
    assert row.vulnerable_victims == 0 and row.rank_score == pytest.approx(expected)
    assert row.f3_stage_max is None and row.merged_into is None
    # Nothing touched: nothing to do. Every case touched again: nothing changes (stable ids).
    assert _recompute(env) == {}
    again = _replay(env)
    assert again["new"] == 0 and again["changed"] == 0 and again["moved_cases"] == 0
    assert _campaign_of(env, a) == campaign


def test_a_merge_keeps_the_oldest_campaign(env: Env) -> None:  # noqa: F811
    first, second = _new_account(env), _new_account(env)
    a, b = _case(env), _case(env, minutes=1)
    _payee(env, a, first)
    _payee(env, b, first)
    _device(env, b, "fp-bridge")  # b's device, set before the job first sees b
    _recompute(env)
    env.clock.advance(timedelta(minutes=5))
    c, d = _case(env, minutes=2), _case(env, minutes=3)
    _payee(env, c, second)
    _payee(env, d, second)
    _recompute(env)
    older, newer = _campaign_of(env, a), _campaign_of(env, c)
    assert older and newer and older != newer
    bridge = _case(env, minutes=1)  # pays the second mule from b's device
    _device(env, bridge, "fp-bridge")
    _payee(env, bridge, second)
    counts = _recompute(env)
    assert counts["merged"] == 1 and counts["touched"] == 1
    assert {_campaign_of(env, x) for x in (a, b, c, d, bridge)} == {older}
    with env.owner.connect() as conn:
        merged = conn.execute(select(campaigns).where(campaigns.c.id == newer)).one()
    assert merged.merged_into == older and merged.case_count == 0


def test_a_disposal_touches_a_case_and_a_genuine_lead_leaves_its_campaign(
    env: Env,  # noqa: F811
) -> None:
    a, b = _case(env), _case(env, minutes=1)
    mule = _new_account(env)
    _payee(env, a, mule)
    _payee(env, b, mule)
    _recompute(env)
    assert _campaign_of(env, a) is not None
    with env.owner.begin() as conn:  # released: the evidence pointed to GENUINE
        conn.execute(
            text(
                "UPDATE delphi.cases SET leading_hypothesis = 'GENUINE', status = 'DISPOSED',"
                " disposition = 'RELEASE', disposed_at = :t WHERE id = :c"
            ),
            {"c": a, "t": T0 + timedelta(hours=1)},
        )
    counts = _recompute(env)
    assert counts["touched"] == 1
    assert _campaign_of(env, a) is None and _campaign_of(env, b) is None  # dissolved


def test_the_component_stops_at_its_limit(env: Env) -> None:  # noqa: F811
    ids = [_case(env, minutes=m) for m in range(4)]
    mule = _new_account(env)
    for case_id in ids:
        _payee(env, case_id, mule)
    _recompute(env, Limits(batch=1, batches_per_run=20, max_component=2))
    with env.owner.connect() as conn:
        cut = conn.execute(text("SELECT count(*) FROM delphi.case_entities")).scalar_one()
    assert cut >= 4  # every case's entities were recorded as it was touched


def test_the_vulnerable_share_raises_the_rank(env: Env) -> None:  # noqa: F811
    a, b = _case(env), _case(env, minutes=1)
    mule = _new_account(env)
    _payee(env, a, mule)
    _payee(env, b, mule)
    with env.owner.begin() as conn:
        conn.execute(
            text(
                "UPDATE bank.customers SET is_vulnerable = true WHERE id = (SELECT a.customer_id"
                " FROM delphi.cases k JOIN bank.payments p ON p.id = k.subject_payment_id"
                " JOIN bank.accounts a ON a.id = p.payer_account_id WHERE k.id = :c)"
            ),
            {"c": a},
        )
    _recompute(env)
    with env.owner.connect() as conn:
        row = conn.execute(select(campaigns)).one()
    assert row.vulnerable_victims == 1
    assert row.rank_score == pytest.approx(
        row.expected_loss_paise * (1 + VULNERABLE_RANK_WEIGHT * 0.5)
    )


def test_cases_further_apart_than_the_window_do_not_link(env: Env) -> None:  # noqa: F811
    a = _case(env)
    b = _case(env, minutes=(WINDOW + 1) * 24 * 60)
    mule = _new_account(env)
    _payee(env, a, mule)
    _payee(env, b, mule)
    assert _recompute(env).get("new", 0) == 0


def test_the_job_skips_while_another_recomputes(env: Env) -> None:  # noqa: F811
    job = CampaignRecomputeJob(env.sessions, env.clock, LIMITS)
    assert job(_Ctx())["more"] is False  # type: ignore[arg-type]
    with env.owner.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(51)"))
        assert job(_Ctx())["skipped"] == "busy"  # type: ignore[arg-type]


@pytest.fixture
def linked(env: Env) -> Iterator[tuple[UUID, list[UUID]]]:  # noqa: F811
    ids = [_case(env, minutes=m) for m in range(3)]
    mule = _new_account(env)
    for case_id in ids:
        _payee(env, case_id, mule)
    _recompute(env)
    campaign = _campaign_of(env, ids[0])
    assert campaign is not None
    yield campaign, ids


def test_the_api_lists_and_shows_a_campaign(
    env: Env,  # noqa: F811
    client: TestClient,  # noqa: F811
    linked: tuple[UUID, list[UUID]],
) -> None:
    campaign, ids = linked
    page = client.get("/api/v1/campaigns").json()
    assert [c["id"] for c in page["items"]] == [str(campaign)] and page["next_cursor"] is None
    detail: Any = client.get(f"/api/v1/campaigns/{campaign}").json()
    assert [c["id"] for c in detail["cases"]] == [str(i) for i in ids]
    [shared] = detail["shared_entities"]
    assert shared["kind"] == "account" and shared["case_count"] == 3
    filtered = client.get("/api/v1/cases", params={"campaign_id": str(campaign)}).json()
    assert {c["id"] for c in filtered["items"]} == {str(i) for i in ids}
    assert all(c["campaign_id"] == str(campaign) for c in filtered["items"])
    assert client.get(f"/api/v1/campaigns/{uuid4()}").status_code == 404
    assert client.get("/api/v1/campaigns", params={"cursor": "nope"}).status_code == 422
