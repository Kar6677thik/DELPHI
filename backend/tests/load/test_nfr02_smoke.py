"""NFR-02 smoke load (docs/03, docs/21 §5, docs/25 M4, #101): Guardian latency, from session
creation to its first response (the fast-path decision, or the first probe prompt), p95 ≤ 400 ms
at 20 sessions/s, held for 2 minutes. The full Locust load test is M9's (docs/21 §5).

It drives a **running stack** the way the mock phone does: it signs in, reads the personas,
listens on each persona's `/phone/stream`, and pays with `POST /guardian/sessions` on a fixed
schedule. A session decided in the response (the fast path) is timed to the response; any other
is timed to its first event on the phone stream (the passive phase, planning and the first
probe). Nobody answers the probes: those sessions time out on their own.

It runs only when asked for (``pytest -m load``), never in CI: ``just smoke-load`` with
``DELPHI_LOAD_URL`` (e.g. ``http://127.0.0.1:8000``), ``DELPHI_LOAD_USERNAME`` and
``DELPHI_LOAD_PASSWORD`` (an ADMIN). It writes about 2,400 payments into the bank and moves the
personas' balances, so run it on a disposable stack, or demo-reset afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import numpy as np
import pytest

TARGET_P95_MS = 400.0  # NFR-02
# NFR-02's arrival rate and docs/25 M4's 2 minutes. A lower rate or a shorter run (to find where
# latency starts to climb) is a diagnosis, not the NFR-02 check.
RATE_PER_S = int(os.environ.get("DELPHI_LOAD_RATE_PER_S", "20"))
DURATION_S = int(os.environ.get("DELPHI_LOAD_DURATION_S", "120"))
FULL_SHARE = 0.4  # docs/21 §5: 40% of the payments look like a full session
# Two signed-in clients share the load, so neither meets the per-session rate limit (docs/16
# §1, 20 requests/s by default) on a schedule that is exactly 20/s.
CLIENTS = 2
FIRST_EVENT_WAIT_S = 30.0  # a session with no response by then counts as a failure
RECONNECT_S = 1.0  # a dropped or refused phone stream is opened again after this
REQUEST_TIMEOUT_S = 30.0
API = "/api/v1"

# Two kinds of payment (docs/08 §5): a small one to a saved payee on a quiet app session, and a
# large one made on a call with remote access on. docs/21 §5 asks for 60% fast-path sessions; the
# KB decides which path each takes, and the report counts both.
FAST_AMOUNT_PAISE = 100
FULL_AMOUNT_PAISE = 48_000_000
QUIET = {
    "typing_cadence_z": 0.0,
    "paste_on_account_field": False,
    "max_field_dwell_s": 4.0,
    "remote_access_active": False,
    "screen_share_active": False,
    "on_call": False,
    "session_duration_s": 40.0,
}
COACHED = {
    **QUIET,
    "typing_cadence_z": -2.5,
    "paste_on_account_field": True,
    "max_field_dwell_s": 45.0,
    "remote_access_active": True,
    "on_call": True,
    "call_counterparty_known": False,
    "session_duration_s": 300.0,
}


@dataclass(frozen=True)
class Payer:
    customer_id: str
    account_id: str
    payee_account_id: str
    device_id: str


@dataclass
class Tally:
    latencies_ms: list[float] = field(default_factory=list)
    fast: int = 0
    full: int = 0
    statuses: dict[int | str, int] = field(default_factory=dict)  # HTTP status or error
    stream_drops: dict[str, int] = field(default_factory=dict)  # why a phone stream reconnected
    no_response: int = 0
    first_event: dict[str, float] = field(default_factory=dict)
    arrived: dict[str, asyncio.Event] = field(default_factory=dict)

    def count(self, outcome: int | str) -> None:
        self.statuses[outcome] = self.statuses.get(outcome, 0) + 1

    def seen(self, session_id: str) -> asyncio.Event:
        return self.arrived.setdefault(session_id, asyncio.Event())


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"set {name} to run the smoke load against a running stack")
    return value


async def _signed_in(base: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        base_url=base,
        headers={"X-Delphi-Csrf": "1", "Origin": base},
        timeout=REQUEST_TIMEOUT_S,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )
    reply = await client.post(
        f"{API}/auth/login",
        json={
            "username": _env("DELPHI_LOAD_USERNAME"),
            "password": _env("DELPHI_LOAD_PASSWORD"),
        },
    )
    assert reply.status_code == httpx.codes.NO_CONTENT, reply.text
    # The session cookie is `Secure`, which httpx keeps off plain http (a local stack): send it
    # as a header, as a browser does on localhost.
    client.cookies.clear()
    client.headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in reply.cookies.items())
    return client


def _payers(personas: list[Mapping[str, Any]]) -> list[Payer]:
    return [
        Payer(
            p["customer_id"],
            p["accounts"][0]["id"],
            p["payees"][0]["account_id"],
            p["devices"][0]["id"],
        )
        for p in personas
        if p["accounts"] and p["payees"] and p["devices"]
    ]


async def _events(client: httpx.AsyncClient, customer_id: str) -> AsyncIterator[dict[str, Any]]:
    async with client.stream(
        "GET", f"{API}/phone/stream", params={"customer_id": customer_id}, timeout=None
    ) as stream:
        stream.raise_for_status()  # e.g. 503 before the runner is up: the listener retries
        async for line in stream.aiter_lines():
            if line.startswith("data:"):
                yield json.loads(line.removeprefix("data:"))


async def _listen(client: httpx.AsyncClient, customer_id: str, tally: Tally) -> None:
    """The phone's stream: when each session first reached the customer. A stream that ends,
    drops or is refused is counted and opened again after a pause (the phone reconnects too)."""
    while True:
        try:
            async for event in _events(client, customer_id):
                session_id = event.get("session_id")
                if session_id is not None and session_id not in tally.first_event:
                    tally.first_event[session_id] = time.perf_counter()
                    tally.seen(session_id).set()
            drop = "ended"
        except httpx.HTTPStatusError as exc:
            drop = str(exc.response.status_code)
        except httpx.HTTPError as exc:
            drop = type(exc).__name__
        tally.stream_drops[drop] = tally.stream_drops.get(drop, 0) + 1
        await asyncio.sleep(RECONNECT_S)


def _payment(payer: Payer, *, full: bool) -> dict[str, Any]:
    return {
        "payment": {
            "payer_account_id": payer.account_id,
            "payee_account_id": payer.payee_account_id,
            "amount_paise": FULL_AMOUNT_PAISE if full else FAST_AMOUNT_PAISE,
            "channel": "UPI_PUSH",
            "device_id": payer.device_id,
            "telemetry": COACHED if full else QUIET,
        }
    }


async def _pay(client: httpx.AsyncClient, payer: Payer, *, full: bool, tally: Tally) -> None:
    start = time.perf_counter()
    try:
        reply = await client.post(
            f"{API}/guardian/sessions",
            json=_payment(payer, full=full),
            headers={"Idempotency-Key": str(uuid4())},
        )
    except httpx.HTTPError as exc:  # e.g. a read timeout: count it, keep the load going
        tally.count(type(exc).__name__)
        return
    answered = time.perf_counter()
    tally.count(reply.status_code)
    if reply.status_code != httpx.codes.CREATED:
        return
    session = reply.json()
    if session["decision"] is not None:  # the fast path: decided in the response
        tally.fast += 1
        tally.latencies_ms.append((answered - start) * 1000)
        return
    tally.full += 1
    try:
        await asyncio.wait_for(tally.seen(session["id"]).wait(), FIRST_EVENT_WAIT_S)
    except TimeoutError:
        tally.no_response += 1
        return
    tally.latencies_ms.append((tally.first_event[session["id"]] - start) * 1000)


async def _run(base: str) -> Tally:
    clients = [await _signed_in(base) for _ in range(CLIENTS)]
    tally = Tally()
    try:
        personas = (await clients[0].get(f"{API}/phone/personas")).raise_for_status().json()
        payers = _payers(personas)
        assert payers, "no persona can pay: seed a dataset with personas"
        listeners = [asyncio.create_task(_listen(clients[0], p.customer_id, tally)) for p in payers]
        await asyncio.sleep(1.0)  # let the streams open before the first payment
        total = RATE_PER_S * DURATION_S
        t0 = time.perf_counter()
        payments = []
        for i in range(total):
            await asyncio.sleep(max(0.0, t0 + i / RATE_PER_S - time.perf_counter()))
            full = int((i + 1) * FULL_SHARE) > int(i * FULL_SHARE)  # spread evenly
            payments.append(
                asyncio.create_task(
                    _pay(clients[i % CLIENTS], payers[i % len(payers)], full=full, tally=tally)
                )
            )
        await asyncio.gather(*payments)
        for task in listeners:
            task.cancel()
        await asyncio.gather(*listeners, return_exceptions=True)
    finally:
        for client in clients:
            await client.aclose()
    return tally


@pytest.mark.load
def test_guardian_first_response_p95_under_load() -> None:
    tally = asyncio.run(_run(_env("DELPHI_LOAD_URL").rstrip("/")))
    timings = np.asarray(tally.latencies_ms)
    p50, p95 = np.percentile(timings, [50, 95]) if timings.size else (float("nan"),) * 2
    print(
        f"\nNFR-02 smoke: {timings.size} sessions timed ({tally.fast} fast path, {tally.full}"
        f" full) at {RATE_PER_S}/s for {DURATION_S} s; first response p50 = {p50:.0f} ms,"
        f" p95 = {p95:.0f} ms, max = {timings.max(initial=0):.0f} ms (target p95"
        f" {TARGET_P95_MS:.0f} ms); HTTP statuses {tally.statuses}; no response"
        f" {tally.no_response}; phone stream reconnects {tally.stream_drops}"
    )
    if tally.fast == 0:  # docs/21 §5 asks for 60% fast path: this run measured none
        print("WARNING: no session took the fast path, so this run timed full sessions only")
    assert set(tally.statuses) == {httpx.codes.CREATED}, f"failed payments: {tally.statuses}"
    assert tally.no_response == 0
    assert p95 <= TARGET_P95_MS
