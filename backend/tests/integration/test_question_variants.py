"""Question variants drafted in the app, their review and "approve batch", on a real database
(docs/07 §9, docs/15, docs/16 "KB", FR-KB-05, #110)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.kb.canonical import content_sha256
from delphi.kb.loader import load_and_validate
from delphi.kb.question_variants import (
    QuestionVariantsJob,
    canonical_variant,
    question_variants,
)
from delphi.kb.store import KbStatus, active_version, ensure_kb_v1, get_version
from delphi.llm.port import LlmRequest, LlmResult, LlmStatus
from delphi.llm.schemas import QuestionVariant, QuestionVariants, VariantOption
from delphi.llm.service import LlmService
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.settings import Settings
from delphi.platform.tables import audit_log, jobs, users
from delphi.platform.wiring import Platform
from delphi.probes.questions import serve_question
from delphi.probes.registry import CLASSIFIER_NAMES

from ._db import MIGRATIONS_DIR, engine_as, upgrade

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
KB_DIR = Path(__file__).resolve().parents[3] / "kb"
KB_V1 = load_and_validate(KB_DIR, CLASSIFIER_NAMES)
PROBE = "PURPOSE_QUESTION"
FDE = Principal(UUID(int=20), frozenset(Permission))
BROWSER = {"X-Delphi-Csrf": "1", "Origin": "http://testserver"}
URL = "/api/v1/question-bank"
SOURCE = canonical_variant(KB_V1, PROBE)


def _draft(language: str, text: str) -> QuestionVariant:
    return QuestionVariant(
        language=cast(Any, language),
        text=text,
        options=[VariantOption(key=o.key, text=f"{language}:{o.text}") for o in SOURCE.options],
    )


DRAFTS = QuestionVariants(
    variants=[
        _draft("te", "ఈ చెల్లింపు దేనికోసం?"),
        _draft("hi", "यह भुगतान किसलिए है?"),
        _draft("ta", "இந்த கட்டணம் எதற்காக?"),  # not asked for: dropped
    ]
)


@dataclass
class FakeLlm:
    """A stand-in provider that returns ``parsed`` and records each request."""

    parsed: Any = field(default_factory=lambda: DRAFTS)
    seen: list[LlmRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    def generate(self, request: LlmRequest) -> LlmResult:
        self.seen.append(request)
        return LlmResult(LlmStatus.OK, self.parsed, "fake", "fake-model", 100, 50, 7)


@dataclass
class Env:
    platform: Platform
    client: TestClient
    engine: Engine
    clock: FixedClock

    def job(self, llm: FakeLlm | None, languages: list[str], job_id: UUID | None = None) -> Any:
        service = LlmService(self.platform.sessions, self.clock, llm, 1_000_000)
        ctx = SimpleNamespace(
            job=SimpleNamespace(
                id=job_id or uuid4(), params={"probe_key": PROBE, "languages": languages}
            )
        )
        return QuestionVariantsJob(self.platform.sessions, self.clock, service)(cast(Any, ctx))

    def pending(self) -> list[dict[str, Any]]:
        body = self.client.get(f"{URL}/variants", params={"status": "PENDING_REVIEW"}).json()
        return cast(list[dict[str, Any]], body["items"])

    def review(self, variant_id: str, **body: Any) -> Any:
        return self.client.post(f"{URL}/variants/{variant_id}/review", json=body)

    def approve_batch(self) -> Any:
        return self.client.post(f"{URL}/approve-batch", headers={"Idempotency-Key": str(uuid4())})


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    url = create_database()
    upgrade(url)
    engine = engine_as(url, "delphi_app")
    clock = FixedClock(T0)
    platform = Platform(
        settings=Settings(database_url=url, migrations_dir=MIGRATIONS_DIR),
        engine=engine,
        sessions=make_session_factory(engine),
        clock=clock,
        started_at=T0,
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    with platform.sessions.begin() as s:
        ensure_kb_v1(s, clock, KB_V1)
        s.execute(  # idempotency keys and jobs point at their user
            users.insert().values(
                id=FDE.user_id,
                username="fde",
                display_name="FDE",
                password_hash="unused",
                role="FDE_ENGINEER",
                disabled=False,
                created_at=T0,
                version=1,
            )
        )
    app = create_app(platform)
    app.dependency_overrides[current_principal] = lambda: FDE
    yield Env(platform, TestClient(app, headers=BROWSER), engine, clock)
    engine.dispose()


def _rows(env: Env) -> list[Any]:
    with env.engine.connect() as conn:
        return list(conn.execute(select(question_variants).order_by(question_variants.c.id)))


def test_the_job_drafts_one_pending_variant_per_language_asked_for(env: Env) -> None:
    llm = FakeLlm()
    job_id = uuid4()
    result = env.job(llm, ["te", "hi"], job_id)
    assert result["drafted"] == 2
    rows = _rows(env)
    assert [(r.language, r.status, r.generated_by) for r in rows] == [
        ("te", "PENDING_REVIEW", "LLM"),
        ("hi", "PENDING_REVIEW", "LLM"),
    ]
    assert all(r.llm_call_id is not None and r.job_id == job_id for r in rows)
    # The model was given the canonical English variant and the languages.
    [request] = llm.seen
    assert request.user_payload["text"] == SOURCE.text
    assert request.user_payload["languages"] == ["te", "hi"]
    # A retry of the same job finds what its earlier attempt committed.
    assert env.job(FakeLlm(), ["te", "hi"], job_id)["drafted"] == 2
    assert len(_rows(env)) == 2


def test_without_a_model_no_variant_is_drafted(env: Env) -> None:
    assert env.job(None, ["te"]) == {
        "drafted": 0,
        "reason": "no model output passed validation",
    }
    assert _rows(env) == []


def test_a_draft_with_other_option_keys_is_refused_by_the_validators(env: Env) -> None:
    wrong = QuestionVariants(
        variants=[
            QuestionVariant(
                language="te", text="?", options=[VariantOption(key="YES", text="అవును")]
            )
        ]
    )
    assert env.job(FakeLlm(parsed=wrong), ["te"])["drafted"] == 0


def test_the_queue_lists_pending_variants_beside_the_english_original(env: Env) -> None:
    env.job(FakeLlm(), ["te", "hi"])
    items = env.pending()
    assert [i["language"] for i in items] == ["te", "hi"]
    assert items[0]["original"]["variant_id"] == SOURCE.variant_id
    assert items[0]["original"]["text"] == SOURCE.text
    page = env.client.get(f"{URL}/variants", params={"limit": 1}).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = env.client.get(f"{URL}/variants", params={"cursor": page["next_cursor"]}).json()
    assert [i["language"] for i in rest["items"]] == ["hi"]


def test_generate_queues_a_job_for_a_question_probe(env: Env) -> None:
    reply = env.client.post(
        f"{URL}/generate",
        json={"probe_key": PROBE, "languages": ["te", "te", "hi"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert reply.status_code == 202
    with env.engine.connect() as conn:
        job = conn.execute(select(jobs).where(jobs.c.id == UUID(reply.json()["job_id"]))).one()
    assert job.type == "LLM_QUESTION_VARIANTS"
    assert job.params == {"probe_key": PROBE, "languages": ["te", "hi"]}
    unknown = env.client.post(
        f"{URL}/generate",
        json={"probe_key": "NO_SUCH_PROBE", "languages": ["te"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert unknown.status_code == 404


def test_a_review_is_audited_and_happens_once(env: Env) -> None:
    env.job(FakeLlm(), ["te", "hi"])
    te, hi = env.pending()
    approved = env.review(te["id"], decision="APPROVE", edited_text="  ఈ డబ్బు ఎందుకు?  ")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["text"] == "ఈ డబ్బు ఎందుకు?"
    assert approved.json()["reviewed_by"] == str(FDE.user_id)
    assert env.review(hi["id"], decision="REJECT").json()["status"] == "REJECTED"
    with env.engine.connect() as conn:
        audited = conn.execute(
            select(audit_log.c.entity_id, audit_log.c.actor_id, audit_log.c.payload)
            .where(audit_log.c.action == "question_variant.reviewed")
            .order_by(audit_log.c.id)
        ).all()
    assert [(a.entity_id, a.actor_id) for a in audited] == [
        (te["id"], str(FDE.user_id)),
        (hi["id"], str(FDE.user_id)),
    ]
    assert audited[0].payload["decision"] == "APPROVE"
    assert audited[0].payload["edited"] is True
    assert audited[0].payload["original_text"] == te["text"]
    assert audited[1].payload["edited"] is False
    # Reviewed once only; unknown → 404.
    assert env.review(te["id"], decision="REJECT").status_code == 409
    assert env.review(str(uuid4()), decision="APPROVE").status_code == 404
    assert env.pending() == []


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"decision": "REJECT", "edited_text": "x"}, 422),
        ({"decision": "APPROVE", "edited_text": "See www.example.com"}, 422),
        ({"decision": "APPROVE", "edited_text": "Call 98765 43210"}, 422),
        ({"decision": "APPROVE", "edited_text": "   "}, 422),
    ],
)
def test_a_bad_edit_is_refused(env: Env, body: dict[str, Any], status: int) -> None:
    env.job(FakeLlm(), ["te"])
    [te] = env.pending()
    assert env.review(te["id"], **body).status_code == status
    assert [v["id"] for v in env.pending()] == [te["id"]]  # still pending


def test_approve_batch_makes_a_candidate_holding_the_approved_variants(env: Env) -> None:
    env.job(FakeLlm(), ["te", "hi"])
    te, hi = env.pending()
    env.review(te["id"], decision="APPROVE")
    env.review(hi["id"], decision="REJECT")
    waiting = {"status": "APPROVED", "included": "false"}
    before = env.client.get(f"{URL}/variants", params=waiting).json()["items"]
    assert [v["id"] for v in before] == [te["id"]]
    reply = env.approve_batch()
    assert reply.status_code == 201, reply.text
    version_id = reply.json()["kb_version_id"]
    assert reply.json()["variant_ids"] == [te["id"]]
    with env.platform.sessions() as s:
        candidate = get_version(s, version_id)
        active = active_version(s)
    assert candidate is not None and active is not None
    assert candidate.status is KbStatus.CANDIDATE and candidate.parent_id == active.id
    added = [v for v in candidate.kb.question_bank[PROBE].variants if v.text == te["text"]]
    assert [(v.language, v.status) for v in added] == [("te", "APPROVED")]
    assert added[0].variant_id.endswith("-te-1")
    assert [o.key for o in added[0].options] == [o.key for o in SOURCE.options]
    # Included once: the next batch has nothing to bundle.
    included = env.client.get(f"{URL}/variants/{te['id']}").json()
    assert included["included_in_kb_version"] == version_id
    assert env.client.get(f"{URL}/variants", params=waiting).json()["items"] == []
    done = env.client.get(f"{URL}/variants", params={"status": "APPROVED", "included": "true"})
    assert [v["id"] for v in done.json()["items"]] == [te["id"]]
    assert env.approve_batch().status_code == 409


def test_pending_and_unpromoted_variants_are_never_served(env: Env) -> None:
    """FR-KB-05: a drafted variant, pending or approved into a CANDIDATE, never reaches a
    customer: the ACTIVE KB, which sessions are pinned to, is unchanged, so a Telugu customer
    still gets an approved KB v1 variant."""
    env.job(FakeLlm(), ["te", "hi"])
    te, _hi = env.pending()
    env.review(te["id"], decision="APPROVE")
    assert env.approve_batch().status_code == 201
    drafted = {r.text for r in _rows(env)}
    with env.platform.sessions() as s:
        active = active_version(s)
    assert active is not None and active.content_sha256 == content_sha256(KB_V1)
    served = serve_question(active.kb.question_bank[PROBE], "te", {})
    assert served.text not in drafted
    assert served.variant_id in {
        v.variant_id for v in KB_V1.question_bank[PROBE].variants if v.status == "APPROVED"
    }
    # Through the API too: the ACTIVE version's question bank holds no drafted text.
    body = env.client.get(f"/api/v1/kb/versions/{active.id}").json()
    assert not any(text in str(body) for text in drafted)
