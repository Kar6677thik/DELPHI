"""One EVIDENCE_NARRATIVE payload per evidence source, in the docs/15 §7 contract (#104)."""

from __future__ import annotations

from typing import Any

AS_OF = "2026-11-11T18:10:00+00:00"
SUBJECT = "ACC-…5678"


def payload(source_key: str, facts: dict[str, Any], record_id: str | None = None) -> dict[str, Any]:
    return {
        "source_key": source_key,
        "record_id": record_id or f"ev-{source_key.lower()}",
        "subject": SUBJECT,
        "as_of": AS_OF,
        "facts": facts,
    }


NARRATIVES: dict[str, dict[str, Any]] = {
    "BENEFICIARY_DEVICE_SHARING": payload(
        "BENEFICIARY_DEVICE_SHARING", {"shared_accounts": 3, "window_days": 30}
    ),
    "INBOUND_FAN_IN": payload("INBOUND_FAN_IN", {"unrelated_payers": 12, "window_h": 48}),
    "PASS_THROUGH_DWELL": payload(
        "PASS_THROUGH_DWELL",
        {"median_dwell_minutes": 17.5, "credits": 9, "window_days": 7, "departure_share": 0.8},
    ),
    "ACCOUNT_PROFILE": payload("ACCOUNT_PROFILE", {"account_age_days": 3, "kyc_level": "MINIMAL"}),
    "LINKED_COMPLAINTS": payload("LINKED_COMPLAINTS", {"complaints": 2}),
    "CRM_CONTACT_HISTORY": payload(
        "CRM_CONTACT_HISTORY", {"event": "SIM_SWAP_REPORTED", "days_ago": 2, "window_days": 14}
    ),
    "LOGIN_GEO_VELOCITY": payload(
        "LOGIN_GEO_VELOCITY",
        {"last_city": "Patna", "speed_kmh": 812.4, "new_city": True, "window_h": 24},
    ),
    "PAYEE_RELATIONSHIP": payload(
        "PAYEE_RELATIONSHIP", {"prior_payments": 4, "max_prior_amount_paise": 48000000}
    ),
    "CUSTOMER_OUTREACH_CALL": payload(
        "CUSTOMER_OUTREACH_CALL", {"result": "CONFIRMS_COERCION", "note": "caller on another line"}
    ),
}
