"""Active probes on the customer channel (docs/08 §4, #94 part 3): what the phone shows, how an
answer becomes the classifier's observation, the OTP protocol, and the client-signal checks.

Pure: no I/O and no clock. Times and codes are passed in.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from delphi.platform.errors import DelphiError, ProblemCode
from delphi.probes.api import AnswerObs, BiometricObs, CoolingOffObs, OtpObs

OTP_DIGITS = 6  # docs/08 §4: a 6-digit code
MS_PER_S = 1000
# docs/08 §4: a client latency more than this much longer than the server saw is refused.
CLIENT_LATENCY_SLACK_S = 10.0
STEP_UPS = frozenset({"OTP_STEP_UP", "BIOMETRIC_STEP_UP"})  # a timeout fails these (docs/05 §2)


class Ui(StrEnum):
    """What the phone renders for a probe (docs/08 §4)."""

    QUESTION = "QUESTION"
    OTP = "OTP"
    BIOMETRIC = "BIOMETRIC"
    COOLING_OFF = "COOLING_OFF"


# Probes are code (ADR-0006): the non-question active probes and their screens.
_UI_BY_PROBE = {
    "OTP_STEP_UP": Ui.OTP,
    "BIOMETRIC_STEP_UP": Ui.BIOMETRIC,
    "COOLING_OFF": Ui.COOLING_OFF,
}


def ui_for(probe_key: str, *, question: bool) -> Ui:
    if question:
        return Ui.QUESTION
    ui = _UI_BY_PROBE.get(probe_key)
    if ui is None:
        raise ValueError(f"{probe_key} has no customer screen")
    return ui


# ---------------------------------------------------------------------- OTP


def new_otp() -> str:
    """A 6-digit code from the CSPRNG (docs/08 §4)."""
    return f"{secrets.randbelow(10**OTP_DIGITS):0{OTP_DIGITS}d}"


def otp_digest(probe_run_id: str, code: str) -> str:
    """What the probe run stores: never the code itself. Keyed by the run, so a digest
    reveals nothing across runs."""
    return hashlib.sha256(f"{probe_run_id}:{code}".encode()).hexdigest()


def otp_matches(probe_run_id: str, code: str, digest: str) -> bool:
    return hmac.compare_digest(otp_digest(probe_run_id, code), digest)


# ---------------------------------------------------------------------- client signals


@dataclass(frozen=True, slots=True)
class ClientSignals:
    shown_at_ms: int
    answered_at_ms: int
    visibility_hidden_count: int = 0
    paste_count: int = 0


def response_latency_s(signals: ClientSignals, server_observed_s: float) -> float:
    """docs/08 §4: ``(answered_at_ms − shown_at_ms) / 1000``, refused (400
    INVALID_CLIENT_SIGNALS) if negative or more than 10 s longer than the server saw."""
    latency = (signals.answered_at_ms - signals.shown_at_ms) / MS_PER_S
    if latency < 0 or latency > server_observed_s + CLIENT_LATENCY_SLACK_S:
        raise DelphiError(
            "The client's timing signals are not plausible.",
            code=ProblemCode.INVALID_CLIENT_SIGNALS,
        )
    if signals.visibility_hidden_count < 0 or signals.paste_count < 0:
        raise DelphiError("Counts cannot be negative.", code=ProblemCode.INVALID_CLIENT_SIGNALS)
    return latency


# ---------------------------------------------------------------------- answers


def _invalid(message: str) -> DelphiError:
    return DelphiError(message, code=ProblemCode.VALIDATION_FAILED)


def _count(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _invalid(f"cooling_off.{key} must be a non-negative integer")
    return value


def question_observation(
    answer: Mapping[str, Any],
    options: frozenset[str],
    signals: ClientSignals,
    latency_s: float,
) -> AnswerObs:
    option = answer.get("option_key")
    if not isinstance(option, str) or option not in options:
        raise _invalid("option_key must be one of the options shown")
    return AnswerObs(option, latency_s, signals.visibility_hidden_count)


def biometric_observation(answer: Mapping[str, Any]) -> BiometricObs:
    result = answer.get("biometric")
    if result not in ("MATCH", "NO_MATCH", "CANCEL"):
        raise _invalid("biometric must be MATCH, NO_MATCH or CANCEL")
    return BiometricObs(result)


def cooling_off_observation(answer: Mapping[str, Any]) -> CoolingOffObs:
    raw = answer.get("cooling_off")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("cancelled"), bool):
        raise _invalid("cooling_off must carry cancelled (true or false)")
    return CoolingOffObs(
        cancelled=raw["cancelled"],
        other_channel_attempts=_count(raw, "other_channel_attempts"),
        limit_increase_requests=_count(raw, "limit_increase_requests"),
        send_now_presses=_count(raw, "send_now_presses"),
    )


def otp_code(answer: Mapping[str, Any]) -> str:
    code = answer.get("otp")
    if not isinstance(code, str) or len(code) != OTP_DIGITS or not code.isdigit():
        raise _invalid("otp must be 6 digits")
    return code


def timed_out_observation(probe_key: str, attempts: int) -> object:
    """A step-up that times out is classified FAILED_OR_ABANDONED (docs/05 §2)."""
    if probe_key == "OTP_STEP_UP":
        return OtpObs(correct=False, attempts=attempts, timed_out=True)
    return BiometricObs("TIMEOUT")
