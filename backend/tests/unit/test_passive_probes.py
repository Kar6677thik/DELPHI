"""Passive probes: telemetry mapping and the parallel runner (docs/08 §2 step 4, FR-GDN-03)."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from delphi.kb.model import ClassifierThresholds, KnowledgeBase
from delphi.probes.classifiers import (
    GUARDIAN_CLASSIFIERS,
    BeneficiaryIntelObs,
    CallStatusObs,
    DevicePostureObs,
    PhoneRiskObs,
    SessionBehaviourObs,
    call_status,
)
from delphi.probes.passive import (
    PassiveRequest,
    PassiveResult,
    PassiveStatus,
    SessionTelemetry,
    call_status_obs,
    device_posture_obs,
    passive_fetchers,
    run_passive_probes,
    session_behaviour_obs,
)

AS_OF = datetime(2026, 11, 9, 23, 40, tzinfo=UTC)
REQUEST = PassiveRequest(uuid4(), uuid4(), uuid4(), uuid4(), AS_OF)
TELEMETRY = SessionTelemetry(
    typing_cadence_z=0.4,
    paste_on_account_field=False,
    max_field_dwell_s=3.0,
    remote_access_active=False,
    screen_share_active=False,
    on_call=True,
    call_counterparty_known=False,
)
FAST_TIMEOUT_MS = 100


@pytest.fixture(scope="module")
def t(kb: KnowledgeBase) -> ClassifierThresholds:
    return kb.config.classifiers


class FakeAdapters:
    """All four ports, answering a coached digital-arrest session (story S-1's passive half)."""

    def beneficiary_intel(self, request: PassiveRequest) -> BeneficiaryIntelObs:
        return BeneficiaryIntelObs(False, 0.2, False, 0, 0, None, request.as_of)

    def phone_risk(self, request: PassiveRequest) -> PhoneRiskObs:
        return PhoneRiskObs("VERY_HIGH")

    def device_posture(self, request: PassiveRequest) -> DevicePostureObs:
        return device_posture_obs(TELEMETRY, request.as_of - timedelta(days=400), request.as_of)

    def session_behaviour(self, request: PassiveRequest) -> SessionBehaviourObs:
        return session_behaviour_obs(TELEMETRY)

    def call_status(self, request: PassiveRequest) -> CallStatusObs:
        return call_status_obs(TELEMETRY)


def _run(
    fetchers: dict[str, object], t: ClassifierThresholds, timeout_ms: int = FAST_TIMEOUT_MS
) -> tuple[PassiveResult, ...]:
    return asyncio.run(run_passive_probes(list(fetchers), fetchers, REQUEST, t, timeout_ms))  # type: ignore[arg-type]


# ---------------------------------------------------------------- telemetry mapping


def test_call_status_off_a_call_ignores_the_contact_flag(t: ClassifierThresholds) -> None:
    obs = call_status_obs(replace(TELEMETRY, on_call=False, call_counterparty_known=True))
    assert obs == CallStatusObs(on_call=False, counterparty_in_contacts=None)
    assert call_status(obs, t) == "NOT_ON_CALL"


@pytest.mark.parametrize(
    ("known", "outcome"),
    [
        (False, "ON_CALL_UNKNOWN_NUMBER"),
        (True, "ON_CALL_KNOWN_CONTACT"),
        (None, "ON_CALL_UNKNOWN_NUMBER"),
    ],
)
def test_call_status_on_a_call_passes_the_contact_status_through(
    t: ClassifierThresholds, known: bool | None, outcome: str
) -> None:
    obs = call_status_obs(replace(TELEMETRY, call_counterparty_known=known))
    assert obs.counterparty_in_contacts is known  # None only when the telemetry says unknown
    assert call_status(obs, t) == outcome


def test_session_behaviour_and_device_posture_copy_the_telemetry() -> None:
    raw = replace(TELEMETRY, paste_on_account_field=True, screen_share_active=True)
    assert session_behaviour_obs(raw) == SessionBehaviourObs(True, 3.0, 0.4)
    seen = AS_OF - timedelta(days=2)
    assert device_posture_obs(raw, seen, AS_OF) == DevicePostureObs(False, True, seen, AS_OF)


# ---------------------------------------------------------------- the runner


def test_fetchers_cover_exactly_the_passive_probes(kb: KnowledgeBase) -> None:
    passive = {
        key
        for key, source in kb.sources.items()
        if key in GUARDIAN_CLASSIFIERS and source.friction_tier == "NONE"
    }
    adapters = FakeAdapters()
    assert set(passive_fetchers(adapters, adapters, adapters, adapters)) == passive


def test_all_passive_probes_classify(t: ClassifierThresholds) -> None:
    adapters = FakeAdapters()
    results = _run(passive_fetchers(adapters, adapters, adapters, adapters), t)  # type: ignore[arg-type]
    assert {r.source_key: r.outcome for r in results} == {
        "BENEFICIARY_INTEL": "CLEAN_UNKNOWN",
        "COUNTERPARTY_PHONE_RISK": "VERY_HIGH",
        "DEVICE_POSTURE": "KNOWN_DEVICE",
        "SESSION_BEHAVIOUR": "NORMAL",
        "CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER",
    }
    assert all(r.status is PassiveStatus.OK and r.error is None for r in results)


def test_results_keep_the_order_of_the_keys(t: ClassifierThresholds) -> None:
    adapters = FakeAdapters()
    fetchers = passive_fetchers(adapters, adapters, adapters, adapters)
    keys = ["CALL_STATUS", "BENEFICIARY_INTEL", "DEVICE_POSTURE"]
    results = asyncio.run(run_passive_probes(keys, fetchers, REQUEST, t, FAST_TIMEOUT_MS))
    assert [r.source_key for r in results] == keys


def test_a_slow_adapter_times_out_without_blocking_the_others(t: ClassifierThresholds) -> None:
    """FR-GDN-03: a slow adapter costs at most the timeout, and the other probes still answer."""
    release = threading.Event()

    def slow(request: PassiveRequest) -> PhoneRiskObs:
        release.wait(timeout=10)
        return PhoneRiskObs("VERY_HIGH")

    adapters = FakeAdapters()
    fetchers = passive_fetchers(adapters, adapters, adapters, adapters)
    fetchers["COUNTERPARTY_PHONE_RISK"] = slow

    async def scenario() -> tuple[tuple[PassiveResult, ...], float]:
        started = time.perf_counter()
        results = await run_passive_probes(list(fetchers), fetchers, REQUEST, t, FAST_TIMEOUT_MS)
        elapsed = time.perf_counter() - started
        release.set()  # let the abandoned thread finish so the event loop can close
        return results, elapsed

    results, elapsed = asyncio.run(scenario())
    by_key = {r.source_key: r for r in results}
    assert by_key["COUNTERPARTY_PHONE_RISK"] == PassiveResult(
        "COUNTERPARTY_PHONE_RISK", PassiveStatus.ERROR, error="TIMEOUT"
    )
    assert all(
        r.status is PassiveStatus.OK for k, r in by_key.items() if k != "COUNTERPARTY_PHONE_RISK"
    )
    assert elapsed < 1.0  # the timeout is 0.1 s; the slow adapter would take 10 s


def test_an_adapter_error_is_an_error_result_not_an_exception(t: ClassifierThresholds) -> None:
    def broken(request: PassiveRequest) -> CallStatusObs:
        raise ConnectionError("telemetry store down")

    results = _run({"CALL_STATUS": broken}, t)
    assert results == (
        PassiveResult(
            "CALL_STATUS", PassiveStatus.ERROR, error="ConnectionError: telemetry store down"
        ),
    )


def test_a_wrong_observation_type_is_an_error_result(t: ClassifierThresholds) -> None:
    results = _run({"CALL_STATUS": lambda request: object()}, t)
    assert results[0].status is PassiveStatus.ERROR
    assert results[0].outcome is None


def test_a_probe_without_an_adapter_or_classifier_is_an_error_result(
    t: ClassifierThresholds,
) -> None:
    results = asyncio.run(
        run_passive_probes(["CALL_STATUS", "NOT_A_PROBE"], {}, REQUEST, t, FAST_TIMEOUT_MS)
    )
    assert [(r.source_key, r.error) for r in results] == [
        ("CALL_STATUS", "NO_ADAPTER"),
        ("NOT_A_PROBE", "NO_ADAPTER"),
    ]


def test_the_kb_timeout_is_300_ms(kb: KnowledgeBase) -> None:
    assert kb.config.guardrails.passive_adapter_timeout_ms == 300
