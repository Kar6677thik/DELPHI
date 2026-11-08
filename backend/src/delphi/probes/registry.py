"""Registry of outcome classifiers that exist in code (docs/07 §3 rule 8).

A KB can only enable sources whose classifier is registered here: probes are code, typologies
are configuration (ADR-0006). The Guardian probes' classifiers live in
``delphi.probes.classifiers`` (docs/08 §5). The Navigator evidence rules arrive with #102
(docs/09 §4); until then their names are listed so KB v1 validates (docs/tech-debt.md TD-2).
"""

from __future__ import annotations

from delphi.probes.classifiers import GUARDIAN_CLASSIFIERS

# Navigator evidence (docs/09 §4): names only until #102 implements their rules.
NAVIGATOR_EVIDENCE_NAMES: frozenset[str] = frozenset(
    {
        "BENEFICIARY_DEVICE_SHARING",
        "INBOUND_FAN_IN",
        "PASS_THROUGH_DWELL",
        "ACCOUNT_PROFILE",
        "LINKED_COMPLAINTS",
        "CRM_CONTACT_HISTORY",
        "LOGIN_GEO_VELOCITY",
        "PAYEE_RELATIONSHIP",
        "CUSTOMER_OUTREACH_CALL",
    }
)

CLASSIFIER_NAMES: frozenset[str] = frozenset(GUARDIAN_CLASSIFIERS) | NAVIGATOR_EVIDENCE_NAMES
