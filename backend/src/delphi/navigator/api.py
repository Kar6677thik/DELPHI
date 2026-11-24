"""The Navigator's facade for the api and the other feature modules (docs/04 §4.1 rule 3).

Guardian calls ``open_case`` when it holds a payment (#94); the api reads cases, builds the
console view and records dispositions (docs/16, #103), and stores the console's view events
(docs/09 §5, #108).
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
from delphi.navigator.ddpack import GuardianTrail, InheritedTrail
from delphi.navigator.ddpack import build as build_case_dd_pack
from delphi.navigator.ddpack import refresh_complete as refresh_dd_pack_complete
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
    unexplained_reviews_query,
)
from delphi.navigator.traces import (
    CLIENT_TYPES,
    MAX_TRACE_BATCH,
    ClientTrace,
    TraceType,
    list_traces,
    record_client_traces,
    record_trace,
)

__all__ = [
    "CLIENT_TYPES",
    "HOLD_DECISIONS",
    "MAX_TRACE_BATCH",
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
    "ClientTrace",
    "ConsoleView",
    "Disposition",
    "EvidenceRecord",
    "EvidenceRow",
    "GuardianTrail",
    "InheritedTrail",
    "PoliceAlert",
    "Recommendation",
    "TraceType",
    "act_on_alert",
    "build_case_dd_pack",
    "case_kpis",
    "case_profile",
    "console",
    "current_records",
    "dispose",
    "get_alert",
    "get_case",
    "list_cases",
    "list_traces",
    "observed_sources",
    "open_case",
    "open_case_for_payment",
    "recommend",
    "record_client_traces",
    "record_trace",
    "refresh_dd_pack_complete",
    "request_evidence",
    "request_simulated_outreach",
    "sort_value",
    "unexplained_reviews_query",
    "vulnerable_payer",
]
