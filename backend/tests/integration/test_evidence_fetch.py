"""The evidence fetch flow on a real database (docs/09 §3, docs/16 "Cases", #102): a request
runs the source's adapter, classifies the facts, updates the case posterior with the KB
likelihoods, stores the record and returns the updated console."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from delphi.api.security import Principal
from delphi.kb.loader import load_and_validate
from delphi.kb.store import KbStatus, insert_version
from delphi.navigator.api import CaseRecord, get_case
from delphi.navigator.tables import evidence_records
from delphi.platform.db import make_session_factory
from delphi.platform.labels import labels
from delphi.platform.tables import audit_log, outbox_events
from delphi.probes.registry import CLASSIFIER_NAMES
from tests.conftest import KB_DIR

from .test_cases import BROWSER, _held_case, env, writer  # noqa: F401 - pytest fixtures
from .test_guardian_store import Env


def _fetch(client: TestClient, case: CaseRecord, **body: Any) -> Any:
    headers = {"Idempotency-Key": str(uuid4()), **BROWSER}
    return client.post(f"/api/v1/cases/{case.id}/evidence-requests", json=body, headers=headers)


def _count(e: Env, statement: Any) -> int:
    with e.owner.connect() as conn:
        return int(conn.execute(statement).scalar_one())


def test_a_fetch_classifies_updates_the_posterior_and_records_it(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, principal = writer
    case = _held_case(env)
    response = _fetch(client, case, source_key="ACCOUNT_PROFILE")
    assert response.status_code == 200, response.text
    view = response.json()
    assert response.headers["etag"] == f'"{case.version + 1}"'
    assert view["status"] == "IN_PROGRESS" and view["version"] == case.version + 1
    [card] = view["evidence_records"]
    # The payee account was opened when the payment was made: new, full KYC (docs/09 §4).
    assert card["outcome"] == "NEW_FULL_KYC" and card["facts"]["kyc_level"] == "FULL"
    assert card["record_refs"] == [str(_payee(env, case))]
    assert card["posterior_before"] != card["posterior_after"]
    assert card["posterior_after"] == view["posterior"]
    assert card["info_yield_bits"] > 0 and card["requested_by"] == str(principal.user_id)
    assert card["rank_at_request"] >= 1 and card["net_value_at_request_paise"] is not None
    assert "ACCOUNT_PROFILE" not in {r["source_key"] for r in view["next_best_evidence"]}
    stored = get_case(env.sessions(), case.id)
    assert stored is not None
    assert stored.posterior == pytest.approx(card["posterior_after"], abs=1e-6)
    assert _count(env, select(func.count()).where(audit_log.c.action == "evidence.recorded")) == 1
    assert (
        _count(
            env,
            select(func.count()).where(
                outbox_events.c.type == "case.updated", outbox_events.c.entity_id == str(case.id)
            ),
        )
        == 1
    )


def _payee(e: Env, case: CaseRecord) -> Any:
    with e.owner.connect() as conn:
        return conn.execute(
            text("SELECT payee_account_id FROM bank.payments WHERE id = :p"),
            {"p": case.subject_payment_id},
        ).scalar_one()


def test_a_source_is_fetched_once_unless_refreshed(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    case = _held_case(env)
    first = _fetch(client, case, source_key="LINKED_COMPLAINTS").json()
    again = _fetch(client, case, source_key="LINKED_COMPLAINTS").json()  # a new Idempotency-Key
    assert again["evidence_records"] == first["evidence_records"]
    assert again["version"] == first["version"]  # nothing changed
    refreshed = _fetch(client, case, source_key="LINKED_COMPLAINTS", refresh=True).json()
    [card] = refreshed["evidence_records"]
    assert card["id"] != first["evidence_records"][0]["id"]
    # The same outcome again replaces the observation: the posterior does not move (#194).
    [was] = first["evidence_records"]
    assert card["outcome"] == was["outcome"]
    assert card["posterior_before"] == pytest.approx(was["posterior_before"], abs=1e-9)
    assert card["posterior_after"] == pytest.approx(was["posterior_after"], abs=1e-9)
    assert refreshed["posterior"] == pytest.approx(first["posterior"], abs=1e-6)
    assert card["info_yield_bits"] == pytest.approx(was["info_yield_bits"], abs=1e-9)
    assert _count(env, select(func.count()).select_from(evidence_records)) == 2
    assert (
        _count(
            env,
            select(func.count()).where(evidence_records.c.superseded_by.is_not(None)),
        )
        == 1
    )


def test_an_outreach_call_needs_its_recorded_outcome_and_labels_coercion(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    case = _held_case(env)
    missing = _fetch(client, case, source_key="CUSTOMER_OUTREACH_CALL")
    assert missing.status_code == 422 and missing.json()["code"] == "VALIDATION_FAILED"
    done = _fetch(
        client,
        case,
        source_key="CUSTOMER_OUTREACH_CALL",
        outcome="CONFIRMS_COERCION",
        note="Customer says a 'CBI officer' is on a video call.",
    )
    assert done.status_code == 200, done.text
    [card] = done.json()["evidence_records"]
    assert card["outcome"] == "CONFIRMS_COERCION" and card["facts"]["note"].startswith("Customer")
    with env.owner.connect() as conn:
        label = conn.execute(
            select(labels.c.hypothesis_key, labels.c.label_source).where(
                labels.c.subject_id == case.subject_payment_id
            )
        ).one()
    assert tuple(label) == ("APP_DIGITAL_ARREST", "OUTREACH_CONFIRMED")  # docs/09 §3


def test_bad_requests_are_refused(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    case = _held_case(env)
    unknown = _fetch(client, case, source_key="CALL_STATUS")  # a Guardian probe, not evidence
    assert unknown.status_code == 422
    with env.owner.begin() as conn:
        conn.execute(
            text(
                "UPDATE delphi.cases SET status = 'DISPOSED', disposition = 'RELEASE',"
                " disposed_at = opened_at WHERE id = :c"
            ),
            {"c": case.id},
        )
    disposed = _fetch(client, case, source_key="ACCOUNT_PROFILE")
    assert disposed.status_code == 409 and disposed.json()["code"] == "INVALID_STATE_TRANSITION"
    missing = client.post(
        f"/api/v1/cases/{uuid4()}/evidence-requests",
        json={"source_key": "ACCOUNT_PROFILE"},
        headers={"Idempotency-Key": str(uuid4()), **BROWSER},
    )
    assert missing.status_code == 404


def test_a_refresh_after_other_evidence_takes_out_only_its_own_observation(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    case = _held_case(env)
    _fetch(client, case, source_key="LINKED_COMPLAINTS")
    after_profile = _fetch(client, case, source_key="ACCOUNT_PROFILE").json()
    again = _fetch(client, case, source_key="LINKED_COMPLAINTS", refresh=True).json()
    assert again["posterior"] == pytest.approx(after_profile["posterior"], abs=1e-6)


def test_a_disabled_source_or_a_stray_recorded_outcome_is_refused(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    client, _ = writer
    case = _held_case(env)
    stray = _fetch(client, case, source_key="ACCOUNT_PROFILE", outcome="CONFIRMS_COERCION")
    assert stray.status_code == 422 and stray.json()["code"] == "VALIDATION_FAILED"
    noted = _fetch(client, case, source_key="ACCOUNT_PROFILE", note="called them")
    assert noted.status_code == 422
    kb = load_and_validate(KB_DIR, CLASSIFIER_NAMES)
    off = kb.sources["LINKED_COMPLAINTS"].model_copy(update={"enabled": False})
    changed = kb.model_copy(update={"sources": {**kb.sources, "LINKED_COMPLAINTS": off}})
    with make_session_factory(env.owner).begin() as s:  # as the owner, like a KB author
        version = insert_version(
            s, env.clock, changed, status=KbStatus.CANDIDATE, change_summary="complaints off"
        )
    with env.owner.begin() as conn:
        conn.execute(
            text("UPDATE delphi.cases SET kb_version_id = :v WHERE id = :c"),
            {"v": version, "c": case.id},
        )
    disabled = _fetch(client, case, source_key="LINKED_COMPLAINTS")
    assert disabled.status_code == 422 and "disabled" in disabled.json()["detail"]
    assert _count(env, select(func.count()).select_from(evidence_records)) == 0


def _outreach_labels(e: Env, case: CaseRecord) -> list[str]:
    with e.owner.connect() as conn:
        return list(
            conn.execute(
                select(labels.c.hypothesis_key).where(
                    labels.c.subject_id == case.subject_payment_id,
                    labels.c.label_source == "OUTREACH_CONFIRMED",
                )
            ).scalars()
        )


def test_a_refreshed_outreach_call_replaces_its_label(
    env: Env,  # noqa: F811 - the imported fixture
    writer: tuple[TestClient, Principal],  # noqa: F811
) -> None:
    """#194 review: a denial (ATO) corrected to coercion must not keep the ATO label."""
    client, _ = writer
    case = _held_case(env)
    call = {"source_key": "CUSTOMER_OUTREACH_CALL"}
    assert _fetch(client, case, **call, outcome="DENIES_INITIATING").status_code == 200
    assert _outreach_labels(env, case) == ["ATO"]
    fixed = _fetch(client, case, **call, outcome="CONFIRMS_COERCION", refresh=True)
    assert fixed.status_code == 200
    assert _outreach_labels(env, case) == ["APP_DIGITAL_ARREST"]
    assert _fetch(client, case, **call, outcome="UNREACHABLE", refresh=True).status_code == 200
    assert _outreach_labels(env, case) == []
