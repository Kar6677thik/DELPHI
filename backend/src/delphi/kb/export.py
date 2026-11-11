"""Export a KB version as the YAML tree it was authored in (docs/07 §1–2, FR-KB-04).

``yaml_tree`` lays the version out exactly as ``delphi.kb.loader`` reads it (``kb.yaml``,
``engine.yaml``, ``regulatory.yaml`` and one file per hypothesis, source, decision profile and
question-bank entry), so loading the export gives back the same version and the same
``content_sha256``. The owner can unzip it over ``kb/`` and commit it by hand; nothing commits
it automatically (no bot commits).

``export_zip`` is byte-for-byte reproducible: entries are sorted and carry a fixed timestamp.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import yaml

from delphi.kb.model import KnowledgeBase

_SOURCE_DIRS = {"PROBE": "probes", "EVIDENCE": "evidence"}
# The zip format's earliest date: a fixed timestamp keeps the archive reproducible.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_HEADER = "# Exported from DELPHI KB version {id} (sha256 {sha}). docs/07-knowledge-base.md\n"


def _yaml(data: Any) -> str:
    text: str = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    return text


def yaml_tree(kb: KnowledgeBase) -> dict[str, str]:
    """Relative path → YAML text, for every file of the KB tree."""
    content = kb.model_dump(mode="json")
    manifest = {
        k: content[k]
        for k in ("kb_format_version", "name", "description", "hypothesis_order", "source_order")
    }
    files = {
        "kb.yaml": _yaml(manifest),
        "engine.yaml": _yaml(content["config"]),
        "regulatory.yaml": _yaml(content["regulatory"]),
    }
    for key, hypothesis in content["hypotheses"].items():
        files[f"hypotheses/{key}.yaml"] = _yaml(hypothesis)
    for key, source in content["sources"].items():
        files[f"sources/{_SOURCE_DIRS[source['kind']]}/{key}.yaml"] = _yaml(source)
    for key, profile in content["decision_profiles"].items():
        files[f"decision_profiles/{key}.yaml"] = _yaml(profile)
    for key, entry in content["question_bank"].items():
        files[f"question_bank/{key}.yaml"] = _yaml(entry)
    return files


def export_zip(kb: KnowledgeBase, *, version_id: int, content_sha256: str) -> bytes:
    """The YAML tree as a zip, each file headed by the version it came from."""
    header = _HEADER.format(id=version_id, sha=content_sha256)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, text in sorted(yaml_tree(kb).items()):
            info = zipfile.ZipInfo(path, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, header + text)
    return buffer.getvalue()
