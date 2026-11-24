"""The Guardian's facade for the api and the other feature modules (docs/04 §4.1 rule 3)."""

from __future__ import annotations

from delphi.guardian.active import ClientSignals
from delphi.guardian.ddpack import DdPack, case_trail
from delphi.guardian.ddpack import build as build_dd_pack
from delphi.guardian.ddpack import render_html as render_dd_pack_html
from delphi.guardian.kpis import GuardianKpis, guardian_kpis
from delphi.guardian.phone import (
    PHONE_EVENT_TYPES,
    LiveGuardian,
    PhoneEvent,
    PhoneHub,
    PhoneSubscription,
    live_guardian,
)
from delphi.guardian.procedure import (
    ProbeStep,
    SessionPrior,
    SessionResult,
    final_decision,
    observe,
    plan_step,
    run_session,
    session_prior,
    stop_reason,
    takes_fast_path,
)
from delphi.guardian.service import PriorPort
from delphi.guardian.store import SessionMode, SessionStatus, TerminalReason
from delphi.guardian.views import (
    SessionCursor,
    SessionFilter,
    SessionRow,
    get_session_row,
    list_sessions,
    probe_run_rows,
    unexplained_sessions_query,
)
from delphi.sensing.api import PriorInputs

__all__ = [
    "PHONE_EVENT_TYPES",
    "ClientSignals",
    "DdPack",
    "GuardianKpis",
    "LiveGuardian",
    "PhoneEvent",
    "PhoneHub",
    "PhoneSubscription",
    "PriorInputs",
    "PriorPort",
    "ProbeStep",
    "SessionCursor",
    "SessionFilter",
    "SessionMode",
    "SessionPrior",
    "SessionResult",
    "SessionRow",
    "SessionStatus",
    "TerminalReason",
    "build_dd_pack",
    "case_trail",
    "final_decision",
    "get_session_row",
    "guardian_kpis",
    "list_sessions",
    "live_guardian",
    "observe",
    "plan_step",
    "probe_run_rows",
    "render_dd_pack_html",
    "run_session",
    "session_prior",
    "stop_reason",
    "takes_fast_path",
    "unexplained_sessions_query",
]
