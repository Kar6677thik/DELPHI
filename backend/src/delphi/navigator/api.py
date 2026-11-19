"""The Navigator's facade for the api and the other feature modules (docs/04 §4.1 rule 3).

Guardian calls ``open_case`` when it holds a payment (#94); the api reads cases, builds the
console view and records dispositions (docs/16, #103).
"""

from __future__ import annotations

from delphi.navigator.console import (
    ConsoleView,
    EvidenceRow,
    case_profile,
    console,
)
from delphi.navigator.disposition import Disposition, dispose, vulnerable_payer
from delphi.navigator.evidence import (
    EvidenceRecord,
    current_records,
    observed_sources,
    request_evidence,
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
)

__all__ = [
    "HOLD_DECISIONS",
    "CaseCursor",
    "CaseExistsError",
    "CaseProtocol",
    "CaseRecord",
    "CaseSort",
    "CaseStatus",
    "CaseType",
    "ConsoleView",
    "Disposition",
    "EvidenceRecord",
    "EvidenceRow",
    "case_profile",
    "console",
    "current_records",
    "dispose",
    "get_case",
    "list_cases",
    "observed_sources",
    "open_case",
    "open_case_for_payment",
    "request_evidence",
    "vulnerable_payer",
]
