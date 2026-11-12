"""The KB diff and the YAML export (docs/16 §2, docs/07 §1, FR-KB-04, #15)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from delphi.kb.canonical import content_sha256
from delphi.kb.diff import Change, DiffOp, diff_content
from delphi.kb.export import export_zip, yaml_tree
from delphi.kb.loader import load_and_validate
from delphi.kb.store import content_of
from delphi.probes.registry import CLASSIFIER_NAMES

KB_DIR = Path(__file__).resolve().parents[3] / "kb"
KB_V1 = load_and_validate(KB_DIR, CLASSIFIER_NAMES)


def test_no_changes_between_equal_contents() -> None:
    assert diff_content(content_of(KB_V1), content_of(KB_V1)) == []


def test_added_removed_and_changed_with_json_pointers() -> None:
    before = {"a": 1, "gone": True, "nested": {"x": [1.0, 2.0], "k/ey": "v"}, "same": [1]}
    after = {"a": 2, "new": "n", "nested": {"x": [1.0, 2.5], "k/ey": "w"}, "same": [1]}
    assert diff_content(before, after) == [
        Change("/a", DiffOp.CHANGED, 1, 2),
        Change("/gone", DiffOp.REMOVED, before=True),
        Change("/nested/k~1ey", DiffOp.CHANGED, "v", "w"),
        Change("/nested/x/1", DiffOp.CHANGED, 2.0, 2.5),
        Change("/new", DiffOp.ADDED, after="n"),
    ]


def test_a_list_that_changed_length_is_one_change() -> None:
    assert diff_content({"order": ["A", "B"]}, {"order": ["A", "B", "C"]}) == [
        Change("/order", DiffOp.CHANGED, ["A", "B"], ["A", "B", "C"])
    ]


def test_a_type_change_is_a_change() -> None:
    assert diff_content({"v": 1}, {"v": "1"}) == [Change("/v", DiffOp.CHANGED, 1, "1")]


def test_a_real_kb_edit_shows_up_where_it_was_made() -> None:
    edited = content_of(KB_V1)
    edited["description"] = "edited"
    assert diff_content(content_of(KB_V1), edited) == [
        Change("/description", DiffOp.CHANGED, KB_V1.description, "edited")
    ]


def test_the_export_reloads_as_the_same_version(tmp_path: Path) -> None:
    for path, text in yaml_tree(KB_V1).items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    reloaded = load_and_validate(tmp_path, CLASSIFIER_NAMES)
    assert reloaded == KB_V1
    assert content_sha256(reloaded) == content_sha256(KB_V1)


def test_the_export_has_the_kb_layout() -> None:
    files = set(yaml_tree(KB_V1))
    expected = {"kb.yaml", "engine.yaml", "regulatory.yaml"}
    expected |= {f"hypotheses/{k}.yaml" for k in KB_V1.hypotheses}
    expected |= {f"decision_profiles/{k}.yaml" for k in KB_V1.decision_profiles}
    expected |= {f"question_bank/{k}.yaml" for k in KB_V1.question_bank}
    expected |= {
        f"sources/{'probes' if s.kind == 'PROBE' else 'evidence'}/{k}.yaml"
        for k, s in KB_V1.sources.items()
    }
    assert files == expected
    # The same files as the repository's kb/ (the README is documentation, not KB content).
    repo = {p.relative_to(KB_DIR).as_posix() for p in KB_DIR.rglob("*.yaml")}
    assert files == repo


def test_the_zip_is_reproducible_and_headed(tmp_path: Path) -> None:
    sha = content_sha256(KB_V1)
    blob = export_zip(KB_V1, version_id=1, content_sha256=sha)
    assert blob == export_zip(KB_V1, version_id=1, content_sha256=sha)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        archive.extractall(tmp_path)
        first = archive.read("kb.yaml").decode()
    assert first.startswith(f"# Exported from DELPHI KB version 1 (sha256 {sha})")
    assert load_and_validate(tmp_path, CLASSIFIER_NAMES) == KB_V1
