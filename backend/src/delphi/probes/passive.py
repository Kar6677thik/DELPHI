"""Passive probes: their ports, the raw-telemetry mapping and the parallel runner (docs/08 §2
step 4, docs/04 §4.2, FR-GDN-03).

Each passive probe reads data that already exists (no customer friction). Its adapter turns
raw data into the probe's observation type (``delphi.probes.classifiers``), and the runner
classifies it. The runner fetches every eligible probe in parallel, each with the
``passive_adapter_timeout_ms`` timeout (kb/engine.yaml). A timeout or an adapter error becomes
an ``ERROR`` result with no outcome, so the session records ``ProbeRun ERROR`` and makes no
update. Nothing raises into the session.

Adapters are synchronous (they run SQL); the runner calls them in worker threads. A thread
that times out keeps running in the background, but the session no longer waits for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from delphi.kb.model import ClassifierThresholds
from delphi.probes.classifiers import (
    GUARDIAN_CLASSIFIERS,
    BeneficiaryIntelObs,
    CallStatusObs,
    DevicePostureObs,
    PhoneRiskObs,
    SessionBehaviourObs,
)

# ---------------------------------------------------------------- request and raw data


@dataclass(frozen=True, slots=True)
class PassiveRequest:
    """What a passive adapter needs to know about the payment being decided."""

    payment_id: UUID
    payer_customer_id: UUID
    payee_account_id: UUID
    device_id: UUID | None
    as_of: datetime  # the payment's initiation time: adapters read only data before it


@dataclass(frozen=True, slots=True)
class SessionTelemetry:
    """The session's raw telemetry, as the mobile SDK (or ``bank.session_telemetry``) reports it."""

    typing_cadence_z: float
    paste_on_account_field: bool
    max_field_dwell_s: float
    remote_access_active: bool
    screen_share_active: bool
    on_call: bool
    call_counterparty_known: bool | None  # None: the contact status is unknown


def session_behaviour_obs(t: SessionTelemetry) -> SessionBehaviourObs:
    return SessionBehaviourObs(
        paste_on_account_field=t.paste_on_account_field,
        max_field_dwell_s=t.max_field_dwell_s,
        typing_cadence_z=t.typing_cadence_z,
    )


def device_posture_obs(
    t: SessionTelemetry, device_first_seen_at: datetime, as_of: datetime
) -> DevicePostureObs:
    return DevicePostureObs(
        remote_access_active=t.remote_access_active,
        screen_share_active=t.screen_share_active,
        device_first_seen_at=device_first_seen_at,
        as_of=as_of,
    )


def call_status_obs(t: SessionTelemetry) -> CallStatusObs:
    """Off a call the contact status does not matter. On a call it is passed through as
    reported: ``None`` only when the telemetry could not tell, which the classifier treats as
    an unknown number (docs/08 §5)."""
    if not t.on_call:
        return CallStatusObs(on_call=False, counterparty_in_contacts=None)
    return CallStatusObs(on_call=True, counterparty_in_contacts=t.call_counterparty_known)


# ---------------------------------------------------------------- ports (docs/04 §4.2)


class BeneficiaryIntelPort(Protocol):
    def beneficiary_intel(self, request: PassiveRequest) -> BeneficiaryIntelObs: ...


class PhoneRiskPort(Protocol):
    def phone_risk(self, request: PassiveRequest) -> PhoneRiskObs: ...


class DeviceTelemetryPort(Protocol):
    def device_posture(self, request: PassiveRequest) -> DevicePostureObs: ...

    def session_behaviour(self, request: PassiveRequest) -> SessionBehaviourObs: ...


class CallStatusPort(Protocol):
    def call_status(self, request: PassiveRequest) -> CallStatusObs: ...


Fetcher = Callable[[PassiveRequest], object]


def passive_fetchers(
    beneficiary: BeneficiaryIntelPort,
    phone: PhoneRiskPort,
    device: DeviceTelemetryPort,
    call: CallStatusPort,
) -> dict[str, Fetcher]:
    """The fetch function for each passive probe in KB v1, keyed by probe key."""
    return {
        "BENEFICIARY_INTEL": beneficiary.beneficiary_intel,
        "COUNTERPARTY_PHONE_RISK": phone.phone_risk,
        "DEVICE_POSTURE": device.device_posture,
        "SESSION_BEHAVIOUR": device.session_behaviour,
        "CALL_STATUS": call.call_status,
    }


# ---------------------------------------------------------------- the runner


class PassiveStatus(StrEnum):
    OK = "OK"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class PassiveResult:
    source_key: str
    status: PassiveStatus
    outcome: str | None = None
    error: str | None = None  # "TIMEOUT", or the exception type and message


async def _run_one(
    key: str,
    fetch: Fetcher | None,
    request: PassiveRequest,
    thresholds: ClassifierThresholds,
    timeout_s: float,
) -> PassiveResult:
    if fetch is None or key not in GUARDIAN_CLASSIFIERS:
        return PassiveResult(key, PassiveStatus.ERROR, error="NO_ADAPTER")
    try:
        obs = await asyncio.wait_for(asyncio.to_thread(fetch, request), timeout_s)
        outcome = GUARDIAN_CLASSIFIERS[key].classify(obs, thresholds)
    except TimeoutError:
        return PassiveResult(key, PassiveStatus.ERROR, error="TIMEOUT")
    except Exception as exc:  # FR-GDN-03: an adapter failure never reaches the session
        return PassiveResult(key, PassiveStatus.ERROR, error=f"{type(exc).__name__}: {exc}")
    return PassiveResult(key, PassiveStatus.OK, outcome=outcome)


async def run_passive_probes(
    keys: Sequence[str],
    fetchers: Mapping[str, Fetcher],
    request: PassiveRequest,
    thresholds: ClassifierThresholds,
    timeout_ms: int,
) -> tuple[PassiveResult, ...]:
    """Fetch and classify ``keys`` in parallel. Results come back in the order of ``keys``
    (pass KB source order: the session applies updates in that order, docs/08 §2)."""
    timeout_s = timeout_ms / 1000.0
    return tuple(
        await asyncio.gather(
            *(_run_one(k, fetchers.get(k), request, thresholds, timeout_s) for k in keys)
        )
    )
