"""Registry of outcome classifiers that exist in code (docs/07 §3 rule 8).

A KB can only enable sources whose classifier is registered here: probes are code, typologies
are configuration (ADR-0006). The Guardian probes' classifiers live in
``delphi.probes.classifiers`` (docs/08 §5) and the Navigator evidence rules in
``delphi.probes.evidence`` (docs/09 §4). ``CUSTOMER_OUTREACH_CALL`` has no rule (the
investigator records its outcome) but is a known source all the same.
"""

from __future__ import annotations

from delphi.probes.classifiers import GUARDIAN_CLASSIFIERS
from delphi.probes.evidence import EVIDENCE_CLASSIFIERS, RECORDED_EVIDENCE

NAVIGATOR_EVIDENCE_NAMES: frozenset[str] = frozenset(EVIDENCE_CLASSIFIERS) | frozenset(
    RECORDED_EVIDENCE
)

CLASSIFIER_NAMES: frozenset[str] = frozenset(GUARDIAN_CLASSIFIERS) | NAVIGATOR_EVIDENCE_NAMES
