"""Session telemetry for a payment, rendered from its §5.1 signals (docs/14 §5.1, docs/08 §5).

A session's signals (``draw_signals``) become ``session_telemetry`` values on the intended side
of the KB's classifier thresholds, so the deterministic classifiers read the same outcomes
back: paste or dwell above the threshold → HESITANT_OR_PASTED; cadence z above it →
CADENCE_MISMATCH; remote access or screen share → REMOTE_ACCESS_ACTIVE; on a call with an
unknown number → ON_CALL_UNKNOWN_NUMBER.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import UUID

import numpy as np

from delphi.kb.model import ClassifierThresholds
from delphi.sim.generator_config import TelemetryShape
from delphi.sim.response_models import SignalDraw
from delphi.sim.rng import quantise

# A normal session's cadence z stays strictly below the mismatch threshold by this margin.
_BELOW_THRESHOLD = 1e-6


@dataclass(frozen=True, slots=True)
class SessionTelemetry:
    payment_id: UUID
    typing_cadence_z: float
    paste_on_account_field: bool
    max_field_dwell_s: float
    remote_access_active: bool
    screen_share_active: bool
    on_call: bool
    call_counterparty_known: bool | None
    session_duration_s: float


def render_telemetry(
    rng: np.random.Generator,
    payment_id: UUID,
    signals: SignalDraw,
    shape: TelemetryShape,
    thresholds: ClassifierThresholds,
) -> SessionTelemetry:
    # Every draw is made for every session, so one signal never shifts another's values.
    u_paste, u_screen, u_dwell = rng.random(3)
    extra_dwell = shape.long_dwell_extra_s.median * math.exp(
        shape.long_dwell_extra_s.sigma * rng.standard_normal()
    )
    excess_z = rng.exponential(shape.cadence_excess_z)
    normal_z = rng.standard_normal()
    duration = shape.session_duration_s.median * math.exp(
        shape.session_duration_s.sigma * rng.standard_normal()
    )

    dwell_threshold = thresholds.paste_or_dwell_max_field_dwell_s
    normal = shape.normal_dwell_s
    normal_dwell = normal.min + (min(normal.max, dwell_threshold) - normal.min) * u_dwell
    paste = signals.paste_or_long_dwell and u_paste < shape.paste_share
    long_dwell = signals.paste_or_long_dwell and not paste
    z_threshold = thresholds.cadence_mismatch_z
    remote = signals.remote_access_active
    screen_share = remote and u_screen < shape.screen_share_share
    return SessionTelemetry(
        payment_id=payment_id,
        typing_cadence_z=quantise(
            z_threshold + excess_z
            if signals.cadence_mismatch
            else min(normal_z, z_threshold - _BELOW_THRESHOLD)
        ),
        paste_on_account_field=bool(paste),
        max_field_dwell_s=quantise(dwell_threshold + extra_dwell if long_dwell else normal_dwell),
        remote_access_active=bool(remote and not screen_share),
        screen_share_active=bool(screen_share),
        on_call=signals.on_call_unknown_number,
        call_counterparty_known=False if signals.on_call_unknown_number else None,
        session_duration_s=quantise(duration),
    )
