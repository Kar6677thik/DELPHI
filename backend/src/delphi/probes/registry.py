"""Registry of outcome classifiers that exist in code (docs/07 §3 rule 8).

A KB can only enable sources whose classifier is registered here: probes are code, typologies
are configuration (ADR-0006). The implementations arrive with M4 (Guardian) and M5 (Navigator);
until then this lists the names they will be registered under (docs/tech-debt.md TD-2).
"""

from __future__ import annotations

CLASSIFIER_NAMES: frozenset[str] = frozenset(
    {
        # Guardian probes (docs/08 §5)
        "BENEFICIARY_INTEL",
        "COUNTERPARTY_PHONE_RISK",
        "DEVICE_POSTURE",
        "SESSION_BEHAVIOUR",
        "CALL_STATUS",
        "OTP_STEP_UP",
        "BIOMETRIC_STEP_UP",
        "PURPOSE_QUESTION",
        "SPELL_BREAKER_DIGITAL_ARREST",
        "SPELL_BREAKER_INVESTMENT",
        "COOLING_OFF",
        "COLLECT_INTENT_CHECK",
        # Navigator evidence (docs/09 §4)
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
