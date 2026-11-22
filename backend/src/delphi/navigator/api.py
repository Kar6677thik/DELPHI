"""The Navigator's facade for the api and the other feature modules (docs/04 §4.1 rule 3).

Guardian calls ``open_case`` when it holds a payment (#94); the api reads cases, builds the
console view and records dispositions (docs/16, #103).
"""

from __future__ import annotations

from delphi.navigator.console import (
    ConsoleView,
    EvidenceRow,
    Recommendation,
    case_profile,
    console,
    recommend,
)
from delphi.navigator.disposition import Disposition, dispose, vulnerable_payer
from delphi.navigator.evidence import (
    OUTREACH,
    EvidenceRecord,
    current_records,
    observed_sources,
    request_evidence,
    request_simulated_outreach,
)
from delphi.navigator.kpis import CaseKpis, case_kpis
from delphi.navigator.police_alert import (
    AlertAction,
    AlertStatus,
    PoliceAlert,
    act_on_alert,
    get_alert,
)
from delphi.navigator.store import (
    HOLD_DECISIONS,
    CaseCursor,
    CaseExistsError,
    CaseProtocol,
    CaseRecord,
    CaseSort,
    CaseStatus,
    CaseType,
    get_case,
    list_cases,
    open_case,
    open_case_for_payment,
    sort_value,
)

__all__ = [
    "HOLD_DECISIONS",
    "OUTREACH",
    "AlertAction",
    "AlertStatus",
    "CaseCursor",
    "CaseExistsError",
    "CaseKpis",
    "CaseProtocol",
    "CaseRecord",
    "CaseSort",
    "CaseStatus",
    "CaseType",
    "ConsoleView",
    "Disposition",
    "EvidenceRecord",
    "EvidenceRow",
    "PoliceAlert",
    "Recommendation",
    "act_on_alert",
    "case_kpis",
    "case_profile",
    "console",
    "current_records",
    "dispose",
    "get_alert",
    "get_case",
    "list_cases",
    "observed_sources",
    "open_case",
    "open_case_for_payment",
    "recommend",
    "request_evidence",
    "request_simulated_outreach",
    "sort_value",
    "vulnerable_payer",
]
