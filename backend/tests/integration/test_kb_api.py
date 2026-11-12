"""The read-only KB API on a real database (docs/16 §2, FR-KB-04, #15): list, get, diff, export."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.kb.canonical import content_sha256
from delphi.kb.loader import load_and_validate
from delphi.kb.store import KbStatus, ensure_kb_v1, insert_version
from delphi.platform.clock import SystemClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform
from delphi.probes.registry import CLASSIFIER_NAMES

from ._db import MIGRATIONS_DIR, engine_as, upgrade

KB_DIR = Path(__file__).resolve().parents[3] / "kb"
KB_V1 = load_and_validate(KB_DIR, CLASSIFIER_NAMES)
EDITED = KB_V1.model_copy(update={"description": "Edited for the diff test."})
VIEWER = Principal(UUID(int=11), frozenset({Permission.VIEW}))
URL = "/api/v1/kb/versions"


@dataclass
class Env:
    client: TestClient
    v1: int
    child: int


@pytest.fixture(scope="module")
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    """KB v1 (ACTIVE) and one CANDIDATE child that edits its description."""
    url = create_database()
    upgrade(url)
    engine = engine_as(url, "delphi_app")
    clock = SystemClock()
    platform = Platform(
        settings=Settings(database_url=url, migrations_dir=MIGRATIONS_DIR),
        engine=engine,
        sessions=make_session_factory(engine),
        clock=clock,
        started_at=clock.now(),
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    with platform.sessions.begin() as s:
        v1 = ensure_kb_v1(s, clock, KB_V1)
        assert v1 is not None
        child = insert_version(
            s, clock, EDITED, status=KbStatus.CANDIDATE, change_summary="edit", parent_id=v1
        )
    app = create_app(platform)
    app.dependency_overrides[current_principal] = lambda: VIEWER
    with TestClient(app) as client:
        yield Env(client, v1, child)
    engine.dispose()


def test_list_is_newest_first_without_content(env: Env) -> None:
    body = env.client.get(URL).json()
    assert [v["id"] for v in body["items"]] == [env.child, env.v1]
    assert body["next_cursor"] is None
    assert all("content" not in v for v in body["items"])
    assert body["items"][1]["status"] == "ACTIVE"
    assert body["items"][0]["parent_id"] == env.v1


def test_list_pages_with_the_cursor(env: Env) -> None:
    first = env.client.get(URL, params={"limit": 1}).json()
    assert [v["id"] for v in first["items"]] == [env.child]
    second = env.client.get(URL, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [v["id"] for v in second["items"]] == [env.v1] and second["next_cursor"] is None
    bad = env.client.get(URL, params={"cursor": "!!"})
    assert bad.status_code == 422 and bad.json()["code"] == "VALIDATION_FAILED"


def test_get_returns_the_content_and_its_hash(env: Env) -> None:
    body = env.client.get(f"{URL}/{env.v1}").json()
    assert body["content_sha256"] == content_sha256(KB_V1)
    assert body["content"]["name"] == KB_V1.name
    assert set(body["content"]) == set(KB_V1.model_dump())
    missing = env.client.get(f"{URL}/9999")
    assert missing.status_code == 404 and missing.json()["code"] == "NOT_FOUND"


def test_diff_defaults_to_the_parent(env: Env) -> None:
    body = env.client.get(f"{URL}/{env.child}/diff").json()
    assert body == {
        "version_id": env.child,
        "against_id": env.v1,
        "changes": [
            {
                "path": "/description",
                "op": "changed",
                "before": KB_V1.description,
                "after": "Edited for the diff test.",
            }
        ],
    }


def test_diff_against_another_version(env: Env) -> None:
    body = env.client.get(f"{URL}/{env.v1}/diff", params={"against": env.child}).json()
    [change] = body["changes"]
    assert (change["before"], change["after"]) == ("Edited for the diff test.", KB_V1.description)
    same = env.client.get(f"{URL}/{env.v1}/diff", params={"against": env.v1}).json()
    assert same["changes"] == []


def test_diff_of_a_root_version_needs_against(env: Env) -> None:
    response = env.client.get(f"{URL}/{env.v1}/diff")
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_FAILED"
    response = env.client.get(f"{URL}/{env.child}/diff", params={"against": 9999})
    assert response.status_code == 404


def test_export_is_a_zip_of_the_kb_tree(env: Env, tmp_path: Path) -> None:
    response = env.client.get(f"{URL}/{env.v1}/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="delphi-kb-v' in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        archive.extractall(tmp_path)
    assert content_sha256(load_and_validate(tmp_path, CLASSIFIER_NAMES)) == content_sha256(KB_V1)
