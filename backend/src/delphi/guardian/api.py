"""The Guardian's facade for the api and the other feature modules (docs/04 §4.1 rule 3)."""

from __future__ import annotations

from delphi.guardian.ddpack import DdPack, case_trail
from delphi.guardian.ddpack import build as build_dd_pack
from delphi.guardian.ddpack import render_html as render_dd_pack_html
from delphi.guardian.kpis import GuardianKpis, guardian_kpis
from delphi.guardian.store import SessionMode, SessionStatus
from delphi.guardian.views import (
    SessionCursor,
    SessionFilter,
    SessionRow,
    UnexplainedSession,
    attributed_sessions,
    get_session_row,
    list_sessions,
    observed_outcomes,
    probe_run_rows,
    unexplained_sessions,
)

__all__ = [
    "DdPack",
    "GuardianKpis",
    "SessionCursor",
    "SessionFilter",
    "SessionMode",
    "SessionRow",
    "SessionStatus",
    "UnexplainedSession",
    "attributed_sessions",
    "build_dd_pack",
    "case_trail",
    "get_session_row",
    "guardian_kpis",
    "list_sessions",
    "observed_outcomes",
    "probe_run_rows",
    "render_dd_pack_html",
    "unexplained_sessions",
]
