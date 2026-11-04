"""The committed ``backend/openapi.json`` matches the code (docs/16 preamble, docs/21 §1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from delphi.api.contract import OPENAPI_FILE, openapi_document
from delphi.cli import main


def test_committed_contract_is_up_to_date() -> None:
    assert OPENAPI_FILE.read_text(encoding="utf-8") == openapi_document(), (
        "backend/openapi.json is out of date: run `uv run delphi openapi` and commit it"
    )


def test_contract_does_not_depend_on_the_build_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELPHI_VERSION", "0123abcd")
    assert json.loads(openapi_document())["info"]["version"] == "dev"


def test_every_operation_documents_problem_json_errors() -> None:
    schema = json.loads(openapi_document())
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            default = operation["responses"]["default"]
            assert set(default["content"]) == {"application/problem+json"}
            ref = default["content"]["application/problem+json"]["schema"]["$ref"]
            assert ref == "#/components/schemas/Problem"
    problem = schema["components"]["schemas"]["Problem"]
    assert set(problem["required"]) == {
        "type",
        "title",
        "status",
        "detail",
        "code",
        "instance",
        "request_id",
    }


def test_cli_writes_and_checks_the_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "openapi.json"
    assert main(["openapi", "--check", "--out", str(out)]) == 1
    assert "out of date" in capsys.readouterr().err
    assert main(["openapi", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == openapi_document()
    assert main(["openapi", "--check", "--out", str(out)]) == 0
    out.write_text("{}\n", encoding="utf-8")
    assert main(["openapi", "--check", "--out", str(out)]) == 1
