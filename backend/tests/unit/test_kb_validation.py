"""KB loading and every docs/07 §3 validation rule, each with a failing fixture."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from delphi.cli import main
from delphi.kb.canonical import canonical_json, content_sha256
from delphi.kb.issues import KbValidationError
from delphi.kb.loader import load_and_validate, load_kb
from delphi.kb.model import KnowledgeBase
from delphi.probes.registry import CLASSIFIER_NAMES
from tests.conftest import KB_DIR


def test_kb_v1_is_valid(kb: KnowledgeBase) -> None:
    assert kb.hypothesis_order == ("GENUINE", "ATO", "APP_DIGITAL_ARREST", "APP_INVESTMENT", "MULE")
    assert len(kb.sources) == 21
    assert not kb.sources["COLLECT_INTENT_CHECK"].enabled
    assert {s.exclusive_group for s in kb.sources.values() if s.exclusive_group} == {"STEP_UP_AUTH"}


def test_hash_is_stable_across_loads() -> None:
    a = load_kb(KB_DIR)
    b = load_kb(KB_DIR)
    assert canonical_json(a) == canonical_json(b)
    assert content_sha256(a) == content_sha256(b)
    assert len(content_sha256(a)) == 64


@pytest.fixture
def kb_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "kb"
    shutil.copytree(KB_DIR, dst)
    return dst


def edit(path: Path, fn: Callable[[Any], None]) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def codes(root: Path, registry: frozenset[str] = CLASSIFIER_NAMES) -> set[str]:
    with pytest.raises(KbValidationError) as exc:
        load_and_validate(root, registry)
    return {i.code for i in exc.value.issues}


def _set_row(h: str, source: str, probs: list[float], ess: float = 20) -> Callable[[Any], None]:
    def fn(d: Any) -> None:
        d["likelihoods"][source] = {"probs": probs, "ess": ess}

    return fn


@pytest.mark.parametrize(
    ("hyp", "source", "probs", "ess", "code"),
    [
        ("ATO", "CALL_STATUS", [0.5, 0.3, 0.3], 20, "LIKELIHOOD_SUM"),
        ("ATO", "CALL_STATUS", [0.5, 0.5], 20, "LIKELIHOOD_LENGTH"),
        ("ATO", "CALL_STATUS", [0.99995, 0.00004, 0.00001], 20, "LIKELIHOOD_FLOOR"),
        ("ATO", "CALL_STATUS", [0.1, 0.1, 0.8], 1, "ESS_RANGE"),
        ("ATO", "CALL_STATUS", [0.0004, 0.0996, 0.9], 20, "ALPHA_FLOOR"),
        ("ATO", "NO_SUCH_SOURCE", [0.5, 0.5], 20, "LIKELIHOOD_UNKNOWN_SOURCE"),
    ],
)
def test_likelihood_rules(
    kb_copy: Path, hyp: str, source: str, probs: list[float], ess: float, code: str
) -> None:
    edit(kb_copy / "hypotheses" / f"{hyp}.yaml", _set_row(hyp, source, probs, ess))
    assert code in codes(kb_copy)


def test_loss_decisions_must_match_profile(kb_copy: Path) -> None:
    edit(
        kb_copy / "hypotheses" / "ATO.yaml", lambda d: d["loss"]["GUARDIAN_PAYMENT"].pop("DECLINE")
    )
    assert "LOSS_DECISIONS_MISMATCH" in codes(kb_copy)


def test_loss_column_required_per_profile(kb_copy: Path) -> None:
    edit(kb_copy / "hypotheses" / "MULE.yaml", lambda d: d["loss"].pop("CASE_ACCOUNT_REVIEW"))
    assert "LOSS_PROFILE_MISSING" in codes(kb_copy)


def test_loss_column_for_inapplicable_profile(kb_copy: Path) -> None:
    edit(
        kb_copy / "hypotheses" / "ATO.yaml",
        lambda d: d["loss"].update(
            CASE_ACCOUNT_REVIEW={"CLEAR": [0, 0, False], "CONFIRM_MULE": [0, 0, False]}
        ),
    )
    assert "LOSS_PROFILE_UNEXPECTED" in codes(kb_copy)


def test_profile_loss_needs_genuine_and_unexplained(kb_copy: Path) -> None:
    edit(
        kb_copy / "decision_profiles" / "GUARDIAN_PAYMENT.yaml",
        lambda d: d["loss"].pop("UNEXPLAINED"),
    )
    assert "PROFILE_LOSS_KEYS" in codes(kb_copy)


def test_open_mass_share_range(kb_copy: Path) -> None:
    edit(kb_copy / "hypotheses" / "ATO.yaml", lambda d: d.update(open_mass_share=0.95))
    assert "OPEN_MASS_SHARE" in codes(kb_copy)


def test_unexplained_must_not_be_defined(kb_copy: Path) -> None:
    text = (kb_copy / "hypotheses" / "ATO.yaml").read_text(encoding="utf-8")
    (kb_copy / "hypotheses" / "UNEXPLAINED.yaml").write_text(
        text.replace("key: ATO", "key: UNEXPLAINED"), encoding="utf-8"
    )
    assert "UNEXPLAINED_DEFINED" in codes(kb_copy)


def test_question_probe_needs_approved_english(kb_copy: Path) -> None:
    def pending(d: Any) -> None:
        for v in d["variants"]:
            v["status"] = "PENDING_REVIEW"

    edit(kb_copy / "question_bank" / "PURPOSE_QUESTION.yaml", pending)
    assert "QUESTION_NO_APPROVED_EN" in codes(kb_copy)


def test_question_options_must_match_probe(kb_copy: Path) -> None:
    edit(
        kb_copy / "question_bank" / "SPELL_BREAKER_INVESTMENT.yaml",
        lambda d: d["variants"][0]["options"].append({"key": "MAYBE", "text": "Maybe"}),
    )
    assert "QUESTION_OPTIONS_MISMATCH" in codes(kb_copy)


def test_classifier_must_exist_in_code(kb_copy: Path) -> None:
    assert "CLASSIFIER_UNKNOWN" in codes(kb_copy, CLASSIFIER_NAMES - {"CALL_STATUS"})


def test_key_must_match_file_name(kb_copy: Path) -> None:
    (kb_copy / "sources" / "probes" / "CALL_STATUS.yaml").rename(
        kb_copy / "sources" / "probes" / "CALL_STATE.yaml"
    )
    assert "KEY_FILENAME_MISMATCH" in codes(kb_copy)


def test_order_must_list_every_file(kb_copy: Path) -> None:
    edit(kb_copy / "kb.yaml", lambda d: d["source_order"].remove("COOLING_OFF"))
    assert "ORDER_MISMATCH" in codes(kb_copy)


def test_keys_must_be_upper_snake(kb_copy: Path) -> None:
    edit(
        kb_copy / "sources" / "evidence" / "INBOUND_FAN_IN.yaml",
        lambda d: d.update(outcomes=["High", "MODERATE", "LOW"]),
    )
    assert "SCHEMA" in codes(kb_copy)


def test_unquoted_yes_no_is_rejected_not_coerced(kb_copy: Path) -> None:
    """YAML 1.1 reads bare YES/NO as booleans; the schema must refuse them, never stringify."""
    path = kb_copy / "sources" / "probes" / "SPELL_BREAKER_INVESTMENT.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('["YES", "NO"]', "[YES, NO]"), encoding="utf-8"
    )
    assert "SCHEMA" in codes(kb_copy)


def test_cli_validate(kb_copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["kb", "validate", str(KB_DIR)]) == 0
    assert "KB valid" in capsys.readouterr().out
    edit(kb_copy / "hypotheses" / "ATO.yaml", lambda d: d.update(open_mass_share=2))
    assert main(["kb", "validate", str(kb_copy)]) == 1
    assert "OPEN_MASS_SHARE" in capsys.readouterr().err
