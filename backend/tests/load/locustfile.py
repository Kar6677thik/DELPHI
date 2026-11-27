"""The M9 load test (docs/21 §5, NFR-02, #130), with Locust, against a running stack.

The scenario: 20 Guardian sessions/s for 10 minutes, paid the way the mock phone pays
(``POST /guardian/sessions``, 40% of them on a call with remote access on, as in the NFR-02
smoke test, ``test_nfr02_smoke.py``), plus 5 investigators browsing the case queue and opening
cases. Nobody answers the probes: those sessions time out on their own.

``just load`` runs it headless (25 users: 20 payers at one payment a second each, and 5
investigators) with ``DELPHI_LOAD_URL``, ``DELPHI_LOAD_USERNAME`` and ``DELPHI_LOAD_PASSWORD``
(an ADMIN), and optionally ``DELPHI_LOAD_METRICS_URL`` (the api's ``/metrics``). It writes about
12,000 payments into the bank and moves the personas' balances: use a disposable stack, or
demo-reset afterwards.

**Pass** (docs/21 §5), checked when the run ends; a failed check makes Locust exit with 1:
- Guardian create p95 ≤ 400 ms (the response: the fast path's decision, or the passive phase);
- no 5xx;
- worker queue lag < 60 s: the oldest QUEUED job's age, sampled every 10 s (``GET /jobs``);
- api memory stable: its resident memory grows < 10% over the run (from ``/metrics``, when
  ``DELPHI_LOAD_METRICS_URL`` is set; otherwise not checked, and the report says so).

The planner's p95 (≤ 50 ms) is not visible from outside the api: no metric exports it. The
planner benchmark measures it (``test_planner_p95``); the report says so. NFR-06's resource fit
needs ``kubectl top`` on the cluster: the owner's server run.
"""

from __future__ import annotations

import os
import random
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import uuid4

import gevent
import requests
from locust import HttpUser, between, constant_throughput, events, task
from locust.env import Environment

API = "/api/v1"
CREATE = "POST /guardian/sessions"
TARGET_CREATE_P95_MS = 400.0  # NFR-02
MAX_QUEUE_LAG_S = 60.0  # docs/21 §5
MAX_MEMORY_GROWTH = 0.10  # docs/21 §5
FULL_SHARE = 0.4  # docs/21 §5: 40% full sessions
LAG_SAMPLE_S = 10.0
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
RSS = re.compile(r"^process_resident_memory_bytes (\S+)$", re.MULTILINE)


class Run:
    """What the run measures besides Locust's own request stats."""

    payers: ClassVar[list[Mapping[str, str]]] = []
    # Signed in once each, up front, and shared: two sessions for the payers (10 payments/s each,
    # under the per-session rate limit of 20 requests/s, docs/16 §1) and one for the
    # investigators, as the NFR-02 smoke test does. 25 sign-ins at once are refused (429).
    payer_cookies: ClassVar[list[str]] = []
    investigator_cookie = ""
    users = 0
    max_queue_lag_s = 0.0
    memory_start: float | None = None
    memory_end: float | None = None


def _use(client: Any, cookie: str) -> None:
    """Send the shared session's cookie as a header: the cookie is `Secure`, which a client
    keeps off plain http (a local stack), as a browser does on localhost."""
    client.headers.update({"Cookie": cookie, "X-Delphi-Csrf": "1", "Origin": client.base_url})


def _memory(url: str | None) -> float | None:
    if not url:
        return None
    found = RSS.search(requests.get(url, timeout=10).text)
    return float(found.group(1)) if found else None


def _cookie(base: str) -> str:
    """Sign in as the load test's ADMIN; the session cookie as a header value."""
    reply = requests.post(
        f"{base}{API}/auth/login",
        json={
            "username": os.environ["DELPHI_LOAD_USERNAME"],
            "password": os.environ["DELPHI_LOAD_PASSWORD"],
        },
        headers={"X-Delphi-Csrf": "1", "Origin": base},
        timeout=10,
    )
    reply.raise_for_status()
    return "; ".join(f"{k}={v}" for k, v in reply.cookies.items())


def _queue_lag(base: str, cookie: str) -> None:
    """Sample the oldest QUEUED job's age every LAG_SAMPLE_S."""
    while True:
        try:
            jobs = requests.get(
                f"{base}{API}/jobs?status=QUEUED&limit=200",
                headers={"Cookie": cookie},
                timeout=10,
            ).json()
            items = jobs.get("items", [])
            if items:
                oldest = min(datetime.fromisoformat(j["created_at"]) for j in items)
                lag = (datetime.now(UTC) - oldest).total_seconds()
                Run.max_queue_lag_s = max(Run.max_queue_lag_s, lag)
        except (requests.RequestException, ValueError, KeyError):
            pass
        gevent.sleep(LAG_SAMPLE_S)


@events.test_start.add_listener
def _start(environment: Environment, **_kw: Any) -> None:
    base = environment.host or os.environ["DELPHI_LOAD_URL"]
    cookie = _cookie(base)
    Run.payer_cookies = [_cookie(base), _cookie(base)]
    Run.investigator_cookie = _cookie(base)
    personas = requests.get(
        f"{base}{API}/phone/personas", headers={"Cookie": cookie}, timeout=10
    ).json()
    Run.payers = [
        {
            "account": p["accounts"][0]["id"],
            "payee": p["payees"][0]["account_id"],
            "device": p["devices"][0]["id"],
        }
        for p in personas
        if p["accounts"] and p["payees"] and p["devices"]
    ]
    if not Run.payers:
        raise RuntimeError("no persona can pay: seed a dataset with the personas first")
    Run.memory_start = _memory(os.environ.get("DELPHI_LOAD_METRICS_URL"))
    gevent.spawn(_queue_lag, base, cookie)


class Payer(HttpUser):
    """One payment a second: 20 payers make NFR-02's 20 sessions/s."""

    fixed_count = 20
    wait_time = constant_throughput(1)

    def on_start(self) -> None:
        Run.users += 1
        _use(self.client, Run.payer_cookies[Run.users % len(Run.payer_cookies)])

    @task
    def pay(self) -> None:
        payer = random.choice(Run.payers)
        full = random.random() < FULL_SHARE
        self.client.post(
            f"{API}/guardian/sessions",
            json={
                "payment": {
                    "payer_account_id": payer["account"],
                    "payee_account_id": payer["payee"],
                    "amount_paise": FULL_AMOUNT_PAISE if full else FAST_AMOUNT_PAISE,
                    "channel": "UPI_PUSH",
                    "device_id": payer["device"],
                    "telemetry": COACHED if full else QUIET,
                }
            },
            headers={"Idempotency-Key": str(uuid4())},
            name=CREATE,
        )


class Investigator(HttpUser):
    """Browses the case queue and opens a case (docs/21 §5: 5 investigators)."""

    fixed_count = 5
    wait_time = between(2, 5)

    def on_start(self) -> None:
        _use(self.client, Run.investigator_cookie)

    @task
    def browse(self) -> None:
        page = self.client.get(f"{API}/cases?limit=50", name="GET /cases").json()
        items = page.get("items", []) if isinstance(page, dict) else []
        if items:
            case = random.choice(items)
            self.client.get(f"{API}/cases/{case['id']}", name="GET /cases/{id}")


def verdict(environment: Environment) -> tuple[bool, list[str]]:
    """docs/21 §5's pass conditions, and a line for each."""
    stats = environment.stats
    create = stats.get(CREATE, "POST")
    p95 = create.get_response_time_percentile(0.95) if create.num_requests else None
    server_errors = sum(
        e.occurrences for e in stats.errors.values() if re.search(r"\b5\d\d\b", str(e.error))
    )
    lines: list[str] = []
    ok = True
    if p95 is None:
        ok = False
        lines.append("FAIL Guardian create: no requests")
    else:
        good = p95 <= TARGET_CREATE_P95_MS
        ok &= good
        lines.append(
            f"{'PASS' if good else 'FAIL'} Guardian create p95 {p95:.0f} ms"
            f" (≤ {TARGET_CREATE_P95_MS:.0f}), {create.num_requests} sessions,"
            f" {create.total_rps:.1f}/s"
        )
    good = server_errors == 0
    ok &= good
    lines.append(f"{'PASS' if good else 'FAIL'} 5xx responses: {server_errors}")
    good = Run.max_queue_lag_s < MAX_QUEUE_LAG_S
    ok &= good
    lines.append(
        f"{'PASS' if good else 'FAIL'} worker queue lag: max {Run.max_queue_lag_s:.0f} s"
        f" (< {MAX_QUEUE_LAG_S:.0f})"
    )
    if Run.memory_start and Run.memory_end:
        growth = Run.memory_end / Run.memory_start - 1
        good = growth < MAX_MEMORY_GROWTH
        ok &= good
        lines.append(
            f"{'PASS' if good else 'FAIL'} api memory: {Run.memory_start / 2**20:.0f} →"
            f" {Run.memory_end / 2**20:.0f} MiB ({growth:+.1%}, < {MAX_MEMORY_GROWTH:.0%})"
        )
    else:
        lines.append("SKIP api memory: set DELPHI_LOAD_METRICS_URL to the api's /metrics")
    lines.append("NOTE planner p95: not exported; the planner benchmark measures it")
    return ok, lines


@events.quitting.add_listener
def _end(environment: Environment, **_kw: Any) -> None:
    Run.memory_end = _memory(os.environ.get("DELPHI_LOAD_METRICS_URL"))
    ok, lines = verdict(environment)
    print(f"\nload test ({time.strftime('%Y-%m-%d %H:%M')}):")
    for line in lines:
        print(f"  {line}")
    if not ok:
        environment.process_exit_code = 1
