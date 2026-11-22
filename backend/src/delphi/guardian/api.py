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
from delphi.guardian.service import PriorPort
from delphi.guardian.store import SessionMode, SessionStatus
from delphi.guardian.views import (
    SessionCursor,
    SessionFilter,
    SessionRow,
    get_session_row,
    list_sessions,
    probe_run_rows,
)

__all__ = [
    "PHONE_EVENT_TYPES",
    "ClientSignals",
    "DdPack",
    "GuardianKpis",
    "LiveGuardian",
    "PhoneEvent",
    "PhoneHub",
    "PhoneSubscription",
    "PriorPort",
    "SessionCursor",
    "SessionFilter",
    "SessionMode",
    "SessionRow",
    "SessionStatus",
    "build_dd_pack",
    "case_trail",
    "get_session_row",
    "guardian_kpis",
    "list_sessions",
    "live_guardian",
    "probe_run_rows",
    "render_dd_pack_html",
]
