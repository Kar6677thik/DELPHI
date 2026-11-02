"""Load a KB YAML tree (docs/07 §2) into an immutable ``KnowledgeBase``.

Loading checks structure (schemas, file names, duplicates). Cross-object rules are in
``delphi.kb.validate``; ``load_and_validate`` runs both.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from delphi.kb.issues import Issue, KbValidationError
from delphi.kb.model import (
    UNEXPLAINED,
    DecisionProfile,
    EngineConfig,
    Hypothesis,
    KnowledgeBase,
    ObservationSource,
    QuestionBankEntry,
    Regulatory,
)
from delphi.kb.validate import validate_kb


def _read_yaml(path: Path, root: Path, issues: list[Issue]) -> Any:
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        issues.append(Issue(_rel(path, root), "YAML_ERROR", str(exc)))
        return None


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _parse[M: BaseModel](
    model: type[M], data: Any, path: Path, root: Path, issues: list[Issue]
) -> M | None:
    if data is None:
        if not any(i.path == _rel(path, root) for i in issues):
            issues.append(Issue(_rel(path, root), "EMPTY_FILE", "file is empty"))
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            issues.append(Issue(f"{_rel(path, root)}:{loc}", "SCHEMA", err["msg"]))
        return None


def _load_keyed[M: BaseModel](
    files: Iterable[Path],
    model: type[M],
    key_of: Callable[[M], str],
    root: Path,
    issues: list[Issue],
) -> dict[str, M]:
    out: dict[str, M] = {}
    for path in sorted(files):
        obj = _parse(model, _read_yaml(path, root, issues), path, root, issues)
        if obj is None:
            continue
        key = key_of(obj)
        if path.stem != key:
            issues.append(
                Issue(_rel(path, root), "KEY_FILENAME_MISMATCH", f"key {key!r} != file name")
            )
        if key in out:
            issues.append(Issue(_rel(path, root), "DUPLICATE_KEY", f"duplicate key {key!r}"))
            continue
        out[key] = obj
    return out


def load_kb(root: Path) -> KnowledgeBase:
    """Parse the YAML tree at ``root``. Raises ``KbValidationError`` on structural problems."""
    root = Path(root)
    issues: list[Issue] = []

    manifest = _read_yaml(root / "kb.yaml", root, issues)
    engine = _parse(
        EngineConfig,
        _read_yaml(root / "engine.yaml", root, issues),
        root / "engine.yaml",
        root,
        issues,
    )
    regulatory = _parse(
        Regulatory,
        _read_yaml(root / "regulatory.yaml", root, issues),
        root / "regulatory.yaml",
        root,
        issues,
    )

    hypotheses = _load_keyed(
        (root / "hypotheses").glob("*.yaml"), Hypothesis, lambda h: h.key, root, issues
    )
    if UNEXPLAINED in hypotheses:  # docs/07 §3 rule 6
        issues.append(
            Issue(
                f"hypotheses/{UNEXPLAINED}.yaml",
                "UNEXPLAINED_DEFINED",
                "UNEXPLAINED is implicit and must not be defined",
            )
        )
        del hypotheses[UNEXPLAINED]

    probe_files = list((root / "sources" / "probes").glob("*.yaml"))
    evidence_files = list((root / "sources" / "evidence").glob("*.yaml"))
    sources = _load_keyed(
        [*probe_files, *evidence_files], ObservationSource, lambda s: s.key, root, issues
    )
    for path in probe_files:
        src = sources.get(path.stem)
        if src is not None and src.kind != "PROBE":
            issues.append(
                Issue(_rel(path, root), "SOURCE_KIND_DIR", "file under probes/ is not a PROBE")
            )
    for path in evidence_files:
        src = sources.get(path.stem)
        if src is not None and src.kind != "EVIDENCE":
            issues.append(
                Issue(_rel(path, root), "SOURCE_KIND_DIR", "file under evidence/ is not EVIDENCE")
            )

    profiles = _load_keyed(
        (root / "decision_profiles").glob("*.yaml"), DecisionProfile, lambda p: p.key, root, issues
    )
    question_bank = _load_keyed(
        (root / "question_bank").glob("*.yaml"),
        QuestionBankEntry,
        lambda q: q.probe_key,
        root,
        issues,
    )

    if issues or engine is None or regulatory is None or not isinstance(manifest, dict):
        if not isinstance(manifest, dict) and not any(i.path == "kb.yaml" for i in issues):
            issues.append(Issue("kb.yaml", "SCHEMA", "manifest must be a mapping"))
        raise KbValidationError(issues)

    data = {
        **manifest,
        "hypotheses": hypotheses,
        "sources": sources,
        "decision_profiles": profiles,
        "question_bank": question_bank,
        "config": engine,
        "regulatory": regulatory,
    }
    kb = _parse(KnowledgeBase, data, root / "kb.yaml", root, issues)
    if kb is None:
        raise KbValidationError(issues)
    return kb


def load_and_validate(root: Path, classifier_registry: frozenset[str]) -> KnowledgeBase:
    """Load and run every docs/07 §3 rule. Raises ``KbValidationError`` with all issues."""
    kb = load_kb(root)
    issues = validate_kb(kb, classifier_registry)
    if issues:
        raise KbValidationError(issues)
    return kb
