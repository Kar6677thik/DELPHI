"""Typology draft editing over HTTP on a real database (docs/11 §3, docs/16 "Scout", FR-SCT-03,
#118): a RUN_SCOUT candidate is read with its ETag and what still stops it, edited with If-Match
into the next revision, and refused with the KB rules it breaks, unsaved."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from delphi.api.security import Permission, Principal, current_principal
from delphi.scout.api import run_scout, typology_drafts

from .test_cases import BROWSER, VIEWER, Env, _investigator, env, writer  # noqa: F401 - fixtures
from .test_scout_queue import _decided
from .test_scout_run import PATTERN, _kb, _with_outcomes

DRAFTS = "/api/v1/typology-drafts"
VALID = {"key": "APP_COLLECT_REFUND", "color": "#c0392b", "f3_stages": ["EXECUTION"]}


@pytest.fixture
def candidate(env: Env) -> UUID:  # noqa: F811
    """RUN_SCOUT on one collect-refund pattern: one CANDIDATE draft."""
    for minute in range(6):
        _with_outcomes(env, _decided(env, unexplained=True, minutes=minute), PATTERN)
    with env.sessions.begin() as s:
        run_scout(s, env.clock, _kb(env), max_items=500)
    with env.owner.connect() as conn:
        draft_id: UUID = conn.execute(select(typology_drafts.c.id)).scalar_one()
    return draft_id


@pytest.fixture
def fde(env: Env, writer: tuple[TestClient, Principal]) -> TestClient:  # noqa: F811
    client, _ = writer
    user = _investigator(env)  # a real user row, for last_edited_by and editors
    client.app.dependency_overrides[current_principal] = lambda: Principal(  # type: ignore[attr-defined]
        user.user_id, frozenset(Permission)
    )
    return client


def _patch(client: TestClient, draft_id: UUID, body: dict[str, Any], etag: str | None) -> Any:
    headers = {**BROWSER, **({"If-Match": etag} if etag is not None else {})}
    return client.patch(f"{DRAFTS}/{draft_id}", json=body, headers=headers)


def test_a_candidate_reads_with_its_etag_and_what_stops_it(
    fde: TestClient, candidate: UUID
) -> None:
    read = fde.get(f"{DRAFTS}/{candidate}")
    assert read.status_code == 200 and read.headers["ETag"] == '"1"'
    view = read.json()
    assert view["status"] == "CANDIDATE" and view["revision"] == 1
    assert view["spec"]["likelihoods"]["PURPOSE_QUESTION"]["ess"] > 0
    assert {i["code"] for i in view["issues"]} == {"DRAFT_KEY", "DRAFT_COLOR"}
    page = fde.get(DRAFTS, params={"status": "CANDIDATE"}).json()
    assert [d["id"] for d in page["items"]] == [str(candidate)] and page["next_cursor"] is None
    assert fde.get(f"{DRAFTS}/{uuid4()}").status_code == 404


def test_an_edit_is_the_next_revision_back_in_draft(fde: TestClient, candidate: UUID) -> None:
    saved = _patch(fde, candidate, VALID, '"1"')
    assert saved.status_code == 200, saved.text
    assert saved.headers["ETag"] == '"2"'
    view = saved.json()
    assert view["status"] == "DRAFT" and view["revision"] == 2 and view["issues"] == []
    assert view["spec"]["key"] == "APP_COLLECT_REFUND"
    assert view["spec"]["open_mass_share"] == 0.5  # docs/11 §3's default
    assert view["spec"]["likelihoods"]["PURPOSE_QUESTION"]  # the cluster's rows are kept
    assert len(view["editors"]) == 1 and view["last_edited_by"] == view["editors"][0]
    # docs/11 §3's example: enable COLLECT_INTENT_CHECK with a row for every hypothesis.
    rows = {
        h: {"probs": [0.05, 0.95], "ess": 20}
        for h in ("GENUINE", "ATO", "APP_DIGITAL_ARREST", "APP_INVESTMENT", "MULE")
    }
    rows["APP_COLLECT_REFUND"] = {"probs": [0.85, 0.15], "ess": 20}
    enabled = _patch(fde, candidate, {"enabled_sources": {"COLLECT_INTENT_CHECK": rows}}, '"2"')
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["revision"] == 3 and len(enabled.json()["editors"]) == 1


def test_an_edit_that_breaks_a_kb_rule_is_refused_unsaved(
    env: Env,  # noqa: F811
    fde: TestClient,
    candidate: UUID,
) -> None:
    bad = {**VALID, "likelihoods": {"PURPOSE_QUESTION": {"probs": [0.5, 0.2, 0.1, 0.1], "ess": 20}}}
    refused = _patch(fde, candidate, bad, '"1"')
    assert refused.status_code == 422
    assert refused.json()["code"] == "VALIDATION_FAILED" and refused.json()["errors"]
    taken = _patch(fde, candidate, {"key": "GENUINE", "color": "#000000"}, '"1"')
    assert taken.status_code == 422
    assert {e["code"] for e in taken.json()["errors"]} == {"DRAFT_KEY"}
    with env.owner.connect() as conn:
        row = conn.execute(select(typology_drafts)).one()
    assert row.revision == 1 and row.status == "CANDIDATE" and row.version == 1  # unsaved


def test_if_match_and_permissions(
    env: Env,  # noqa: F811
    writer: tuple[TestClient, Principal],  # noqa: F811
    candidate: UUID,
) -> None:
    client, _ = writer  # an investigator: no `fde`
    assert _patch(client, candidate, VALID, '"1"').status_code == 403
    client.app.dependency_overrides[current_principal] = lambda: VIEWER  # type: ignore[attr-defined]
    assert client.get(f"{DRAFTS}/{candidate}").status_code == 200
    user = _investigator(env)
    client.app.dependency_overrides[current_principal] = lambda: Principal(  # type: ignore[attr-defined]
        user.user_id, frozenset(Permission)
    )
    assert _patch(client, candidate, VALID, None).status_code == 422  # no If-Match
    assert _patch(client, candidate, VALID, '"7"').status_code == 412  # stale
    assert _patch(client, candidate, VALID, '"1"').status_code == 200
    with env.owner.begin() as conn:
        conn.execute(
            typology_drafts.update().values(status="REJECTED", rejected_reason="not a pattern")
        )
    assert _patch(client, candidate, {"description": "x"}, '"2"').status_code == 409
