"""The Guardian's write side and the mock phone on a real database (docs/16 "Guardian",
docs/08 §2, §4, FR-GDN-07, FR-KB-05, #98).

The api's runner slot holds a ``LiveGuardian`` over the real service with a fixture prior and
fixture passive outcomes (the sensing inference is #92's). Requests go through the app; the
phone stream is read from a real uvicorn server, since an SSE response never ends.
"""

from __future__ import annotations

import asyncio
import json
import queue
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from delphi.api.app import create_app
from delphi.api.security import Principal, current_principal
from delphi.guardian.active import Ui
from delphi.guardian.api import LiveGuardian, live_guardian
from delphi.guardian.phone import PhoneChannel, PhoneHub
from delphi.guardian.service import IssuedProbe
from delphi.guardian.store import Runner, SessionMode, get_session
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase, QuestionVariant
from delphi.kb.store import ensure_kb_v1
from delphi.platform.accounts import Role, create_user
from delphi.platform.audit import ActorType
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sim import api as bank

from ._db import MIGRATIONS_DIR, engine_as, upgrade
from .test_guardian_active import KAMALA_ANSWERS, KAMALA_PASSIVE, KAMALA_PRIOR, story_prior
from .test_guardian_service import CLEAN, GENUINE_SURE, INR, KB_DIR, T0, Env, FixedPrior

API = "/api/v1/guardian/sessions"
PHONE = "/api/v1/phone"
BROWSER = {"X-Delphi-Csrf": "1", "Origin": "http://testserver"}
SHOWN_MS = 1_731_000_000_000
WAIT_S = 30.0

# FR-KB-05: unreviewed variants that sort first (ties go to the lowest variant_id, docs/07
# §9), in the customer's language and in the English fallback. None may ever be shown.
UNREVIEWED = {
    "PURPOSE_QUESTION": [("PQ-en-0", "en", "PENDING_REVIEW"), ("PQ-te-0", "te", "REJECTED")],
    "SPELL_BREAKER_DIGITAL_ARREST": [
        ("SBDA-en-0", "en", "REJECTED"),
        ("SBDA-te-0", "te", "PENDING_REVIEW"),
    ],
}
UNREVIEWED_TEXT = "UNREVIEWED DRAFT"


def _with_unreviewed(kb: KnowledgeBase) -> KnowledgeBase:
    bank_ = dict(kb.question_bank)
    for probe, extra in UNREVIEWED.items():
        entry = bank_[probe]
        model = entry.variants[0]
        drafts = tuple(
            QuestionVariant(
                variant_id=vid,
                language=lang,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                text=UNREVIEWED_TEXT,
                options=model.options,
            )
            for vid, lang, status in extra
        )
        bank_[probe] = entry.model_copy(update={"variants": (*drafts, *entry.variants)})
    return kb.model_copy(update={"question_bank": bank_})


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    """A migrated database whose ACTIVE KB v1 also holds unreviewed question variants."""
    url = create_database()
    upgrade(url)
    owner = engine_as(url)
    app_engine = engine_as(url, "delphi_app")
    clock = FixedClock(T0)
    kb = _with_unreviewed(load_and_validate(KB_DIR, CLASSIFIER_NAMES))
    with make_session_factory(owner).begin() as s:
        assert ensure_kb_v1(s, clock, kb) is not None
    yield Env(owner, make_session_factory(app_engine), clock)
    app_engine.dispose()
    owner.dispose()


@dataclass(frozen=True)
class Persona:
    customer: UUID
    payer: UUID
    payee: UUID
    payee_vpa: str
    device: UUID
    strangers_device: UUID


def _persona(env: Env) -> Persona:
    """Kamala (docs/14 §6), as the seed writes her: a persona customer with an account, a
    saved payee, a phone; plus an unknown payee by VPA and another customer's phone."""
    customer, other = uuid4(), uuid4()
    payer, saved, payee = uuid4(), uuid4(), uuid4()
    device, strangers = uuid4(), uuid4()
    with env.owner.begin() as conn:
        for cid, key, name in ((customer, "KAMALA", "Kamala Devi"), (other, None, "Customer 1")):
            conn.execute(
                text(
                    "INSERT INTO bank.customers (id, display_name, age, language, is_vulnerable,"
                    " segment, income_band, consent_call_status, home_city, persona_key)"
                    " VALUES (:c, :n, 68, 'te', true, 'RETAIL', 'MID', true, 'Hyderabad', :k)"
                ),
                {"c": cid, "n": name, "k": key},
            )
        for account, owner, bank_code, vpa, balance in (
            (payer, customer, "DLPH", "customer000001@dlph", 900_000),
            (saved, None, "XBK1", "person000001@xbk1", 0),
            (payee, None, "XBK2", "person000002@xbk2", 0),
        ):
            conn.execute(
                text(
                    "INSERT INTO bank.accounts (id, customer_id, bank_code, opened_at,"
                    " kyc_level, status, balance_paise, vpa)"
                    " VALUES (:a, :c, :b, :t, 'FULL', 'ACTIVE', :m, :v)"
                ),
                {
                    "a": account,
                    "c": owner,
                    "b": bank_code,
                    "t": T0 - timedelta(days=2000),
                    "m": balance * INR,
                    "v": vpa,
                },
            )
        conn.execute(
            text(
                "INSERT INTO bank.payees (id, customer_id, account_id, label, added_at)"
                " VALUES (:i, :c, :a, 'Family 1', :t)"
            ),
            {"i": uuid4(), "c": customer, "a": saved, "t": T0 - timedelta(days=700)},
        )
        for dev, cid in ((device, customer), (strangers, other)):
            conn.execute(
                text(
                    "INSERT INTO bank.devices (id, customer_id, fingerprint, os, first_seen_at)"
                    " VALUES (:d, :c, :f, 'ANDROID', :t)"
                ),
                {"d": dev, "c": cid, "f": str(dev), "t": T0 - timedelta(days=900)},
            )
    return Persona(customer, payer, payee, "person000002@xbk2", device, strangers)


def _user(env: Env, role: Role) -> Principal:
    """A real user (idempotency keys belong to one) with the role's permissions."""
    with env.sessions.begin() as s:
        user = create_user(
            s,
            env.clock,
            username=f"{role.value.lower()}-{uuid4().hex[:6]}",
            display_name=role.value,
            password="correct horse battery",
            role=role,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
        )
    return Principal.of(user)


def _platform(env: Env) -> Platform:
    return Platform(
        settings=Settings(database_url="postgresql://unused/db", migrations_dir=MIGRATIONS_DIR),
        engine=env.owner,
        sessions=env.sessions,
        clock=env.clock,
        started_at=T0,
        migration_head=migration_head(MIGRATIONS_DIR),
    )


def _live(env: Env, prior: FixedPrior, outcomes: Mapping[str, str]) -> LiveGuardian:
    return live_guardian(env.service(prior, outcomes, explore=False), env.sessions, env.clock)


def _app(env: Env, principal: Principal, live: LiveGuardian | None) -> FastAPI:
    app = create_app(_platform(env), guardian_runner=live)
    app.dependency_overrides[current_principal] = lambda: principal
    return app


def _payment(p: Persona, amount_inr: int = 480_000, **changes: Any) -> dict[str, Any]:
    payment: dict[str, Any] = {
        "payer_account_id": str(p.payer),
        "payee_account_id": str(p.payee),
        "amount_paise": amount_inr * INR,
        "channel": "IMPS",
        "device_id": str(p.device),
        "telemetry": {
            "typing_cadence_z": 0.3,
            "paste_on_account_field": False,
            "max_field_dwell_s": 6.0,
            "remote_access_active": False,
            "screen_share_active": False,
            "on_call": True,
            "call_counterparty_known": False,
            "session_duration_s": 95.0,
        },
    }
    payment.update(changes)
    return {k: v for k, v in payment.items() if v is not None}


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


def _signals(env: Env, latency_s: float, switches: int = 0) -> dict[str, int]:
    """The phone's timing; the server's clock moves on by the same time."""
    env.clock.advance(timedelta(seconds=latency_s))
    return {
        "shown_at_ms": SHOWN_MS,
        "answered_at_ms": SHOWN_MS + round(latency_s * 1000),
        "visibility_hidden_count": switches,
        "paste_count": 0,
    }


def _until(
    get: Callable[[], dict[str, Any]], done: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    deadline = time.monotonic() + WAIT_S
    while True:
        current = get()
        if done(current):
            return current
        assert time.monotonic() < deadline, f"gave up waiting: {current.get('status')}"
        time.sleep(0.05)


def _waiting(view: dict[str, Any]) -> dict[str, Any] | None:
    return next((r for r in view["probe_runs"] if r["status"] == "AWAITING_CUSTOMER"), None)


# ---------------------------------------------------------------- the slot, access, validation


def test_without_a_runner_the_write_endpoints_answer_not_initialised(env: Env) -> None:
    p = _persona(env)
    app = _app(env, _user(env, Role.INVESTIGATOR), None)
    with TestClient(app, headers=BROWSER) as client:
        sid = uuid4()
        answer = {
            "probe_run_id": str(uuid4()),
            "answer": {"option_key": "NO"},
            "client_signals": {"shown_at_ms": 1, "answered_at_ms": 2},
        }
        replies = [
            client.post(API, json={"payment": _payment(p)}, headers=_key()),
            client.post(f"{API}/{sid}/probe-responses", json=answer, headers=_key()),
            client.post(f"{API}/{sid}/cancel"),
            client.get(f"{PHONE}/stream", params={"customer_id": str(p.customer)}),
        ]
        for reply in replies:
            assert reply.status_code == 503, reply.text
            assert reply.json()["code"] == "NOT_INITIALISED"
        # the personas need no runner
        assert client.get(f"{PHONE}/personas").status_code == 200
    with env.owner.connect() as conn:  # nothing was written
        assert conn.execute(text("SELECT count(*) FROM bank.payments")).scalar_one() == 0


def test_a_viewer_cannot_drive_the_phone(env: Env) -> None:
    p = _persona(env)
    live = _live(env, FixedPrior(GENUINE_SURE, p_mule=None), CLEAN)
    viewer = _user(env, Role.VIEWER)
    with TestClient(_app(env, viewer, live), headers=BROWSER) as client:
        sid = uuid4()
        replies = [
            client.post(API, json={"payment": _payment(p)}, headers=_key()),
            client.post(f"{API}/{sid}/cancel"),
            client.get(f"{PHONE}/personas"),
            client.get(f"{PHONE}/stream", params={"customer_id": str(p.customer)}),
        ]
    assert [r.status_code for r in replies] == [403] * 4


def test_the_personas_carry_their_accounts_payees_and_devices(env: Env) -> None:
    p = _persona(env)
    app = _app(env, _user(env, Role.INVESTIGATOR), None)
    with TestClient(app, headers=BROWSER) as client:
        [kamala] = client.get(f"{PHONE}/personas").json()
    assert kamala["customer_id"] == str(p.customer) and kamala["persona_key"] == "KAMALA"
    assert (kamala["name"], kamala["language"], kamala["vulnerable"]) == ("Kamala Devi", "te", True)
    assert kamala["accounts"] == [
        {
            "id": str(p.payer),
            "label": "customer000001@dlph",
            "vpa": "customer000001@dlph",
            "balance_paise": 900_000 * INR,
        }
    ]
    assert [(y["label"], y["vpa"]) for y in kamala["payees"]] == [("Family 1", "person000001@xbk1")]
    assert [(d["id"], d["label"]) for d in kamala["devices"]] == [(str(p.device), "ANDROID phone")]


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"amount_paise": 10_000_000 * 100 + 1}, "over ₹10 crore (docs/19 §4)"),
        ({"amount_paise": 0}, "nothing to pay"),
        ({"amount_paise": 1500.5}, "paise are integers"),
        ({"channel": "CASH_OUT"}, "not a channel the app pays on"),
        ({"new_payee": {"vpa": "person000002@xbk2"}}, "two payees"),
        ({"payee_account_id": None}, "no payee"),
        ({"surprise": True}, "an unknown field"),
        ({"telemetry": {"on_call": True}}, "partial telemetry"),
    ],
)
def test_a_malformed_payment_is_refused(env: Env, changes: dict[str, Any], why: str) -> None:
    p = _persona(env)
    live = _live(env, FixedPrior(GENUINE_SURE, p_mule=None), CLEAN)
    with TestClient(_app(env, _user(env, Role.INVESTIGATOR), live), headers=BROWSER) as client:
        reply = client.post(API, json={"payment": _payment(p, **changes)}, headers=_key())
    assert reply.status_code == 422, why


def test_the_bank_refuses_what_the_customer_cannot_pay_from_or_to(env: Env) -> None:
    p = _persona(env)
    live = _live(env, FixedPrior(GENUINE_SURE, p_mule=None), CLEAN)
    cases = [
        {"device_id": str(p.strangers_device)},  # someone else's phone
        {"payee_account_id": None, "new_payee": {"vpa": "nobody@nowhere"}},  # unknown VPA
        {"payee_account_id": str(p.payer)},  # to itself
        {"payer_account_id": str(p.payee)},  # not a customer's account here
        {"payee_account_id": str(uuid4())},  # no such account
    ]
    with TestClient(_app(env, _user(env, Role.INVESTIGATOR), live), headers=BROWSER) as client:
        for changes in cases:
            reply = client.post(API, json={"payment": _payment(p, **changes)}, headers=_key())
            assert reply.status_code == 422, changes
            assert reply.json()["code"] == "VALIDATION_FAILED"
        missing_key = client.post(API, json={"payment": _payment(p)})
        assert missing_key.status_code == 422
    with env.owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM bank.payments")).scalar_one() == 0


# ---------------------------------------------------------------- start, replay, fast path


def test_a_payment_starts_its_session_once_per_key(env: Env) -> None:
    """FR-GDN-01 fast path from the phone; the same key replays, another body conflicts."""
    p = _persona(env)
    live = _live(env, FixedPrior(GENUINE_SURE, p_mule=None), CLEAN)
    body = {"payment": _payment(p, 2_000, payee_account_id=None, new_payee={"vpa": p.payee_vpa})}
    headers = _key()
    with TestClient(_app(env, _user(env, Role.INVESTIGATOR), live), headers=BROWSER) as client:
        first = client.post(API, json=body, headers=headers)
        again = client.post(API, json=body, headers=headers)
        other = client.post(API, json={"payment": _payment(p, 3_000)}, headers=headers)
    assert first.status_code == 201, first.text
    view = first.json()
    assert view["status"] == "DECIDED" and view["decision"] == "RELEASE"
    assert view["terminal_reason"] == "FAST_PATH"
    assert first.headers["etag"] == f'"{view["version"]}"'
    assert view["payment"]["payee_account_id"] == str(p.payee)  # the VPA, resolved
    assert (again.status_code, again.json()) == (201, view)
    assert again.headers["idempotent-replayed"] == "true"
    assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_CONFLICT"
    with env.owner.connect() as conn:
        rows = conn.execute(
            select(bank.payments.c.origin, bank.payments.c.status, bank.payments.c.device_id)
        ).all()
        telemetry = conn.execute(select(bank.session_telemetry.c.on_call)).scalars().all()
    assert rows == [("DEMO", "EXECUTED", p.device)]  # one payment, released and executed
    assert telemetry == [True]


# ---------------------------------------------------------------- answering and cancelling


def test_the_phone_answers_each_probe_until_the_session_decides(env: Env) -> None:
    """S-1 from the phone (docs/21 ENG-S1's answers): purpose, spell-breaker, protective
    hold. Each answer is idempotent; wrong runs, stale runs and bad answers are refused."""
    p = _persona(env)
    live = _live(env, story_prior(KAMALA_PRIOR), KAMALA_PASSIVE)
    with TestClient(_app(env, _user(env, Role.INVESTIGATOR), live), headers=BROWSER) as client:
        created = client.post(API, json={"payment": _payment(p)}, headers=_key())
        assert created.status_code == 201, created.text
        sid = created.json()["id"]

        def read() -> dict[str, Any]:
            return dict(client.get(f"{API}/{sid}").json())

        asked: list[str] = []
        answered: list[str] = []
        while True:
            view = _until(read, lambda v: v["status"] in ("AWAITING_CUSTOMER", "DECIDED"))
            run = _waiting(view)
            if view["status"] == "DECIDED" or run is None:
                break
            asked.append(run["probe_key"])
            path = f"{API}/{sid}/probe-responses"
            if not answered:  # the refusals, once, on a waiting run
                bad = {"probe_run_id": run["id"], "answer": {"option_key": "NOT_AN_OPTION"}}
                bad["client_signals"] = _signals(env, 1.0)
                assert client.post(path, json=bad, headers=_key()).status_code == 422
                stray = dict(bad, probe_run_id=str(uuid4()))
                assert client.post(path, json=stray, headers=_key()).status_code == 404
                elsewhere = client.post(
                    f"{API}/{uuid4()}/probe-responses", json=bad, headers=_key()
                )
                assert elsewhere.status_code == 404
            answer, latency, switches = KAMALA_ANSWERS[run["probe_key"]]
            body = {
                "probe_run_id": run["id"],
                "answer": answer,
                "client_signals": _signals(env, latency, switches),
            }
            headers = _key()
            reply = client.post(path, json=body, headers=headers)
            assert reply.status_code == 200, reply.text
            assert reply.json()["attempts_remaining"] is None
            replay = client.post(path, json=body, headers=headers)
            assert replay.headers["idempotent-replayed"] == "true"
            assert replay.json() == reply.json()
            late = client.post(path, json=body, headers=_key())  # a second answer to it
            assert late.status_code == 409 and late.json()["code"] == "PROBE_NOT_AWAITING"
            answered.append(run["id"])
        final = read()
    assert asked[:2] == ["PURPOSE_QUESTION", "SPELL_BREAKER_DIGITAL_ARREST"]
    assert final["decision"] == "PROTECTIVE_HOLD"
    assert env.payment_row(UUID(final["payment"]["id"])).status == "HELD"


def test_the_customer_cancels_while_a_probe_waits(env: Env) -> None:
    """FR-GDN-07: the session ends CANCELLED_BY_CUSTOMER, the payment CANCELLED, and the
    session's task stops at once rather than at the probe's deadline."""
    p = _persona(env)
    live = _live(env, story_prior(KAMALA_PRIOR), KAMALA_PASSIVE)
    with TestClient(_app(env, _user(env, Role.INVESTIGATOR), live), headers=BROWSER) as client:
        sid = client.post(API, json={"payment": _payment(p)}, headers=_key()).json()["id"]
        _until(
            lambda: dict(client.get(f"{API}/{sid}").json()),
            lambda v: v["status"] == "AWAITING_CUSTOMER",
        )
        cancelled = client.post(f"{API}/{sid}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        view = cancelled.json()
        assert view["status"] == "CANCELLED_BY_CUSTOMER" and view["decision"] is None
        assert cancelled.headers["etag"] == f'"{view["version"]}"'
        deadline = time.monotonic() + 5  # far less than the 60 s probe timeout
        while live.runner.task(UUID(sid)) is not None:
            assert time.monotonic() < deadline, "the session task did not stop"
            time.sleep(0.05)
        again = client.post(f"{API}/{sid}/cancel")
        unknown = client.post(f"{API}/{uuid4()}/cancel")
    assert again.status_code == 409 and again.json()["code"] == "INVALID_STATE_TRANSITION"
    assert unknown.status_code == 404
    assert env.payment_row(UUID(view["payment"]["id"])).status == "CANCELLED"


def test_a_session_the_simulation_drives_is_not_the_phones(env: Env) -> None:
    p = _persona(env)
    live = _live(env, story_prior(KAMALA_PRIOR), KAMALA_PASSIVE)
    with TestClient(_app(env, _user(env, Role.INVESTIGATOR), live), headers=BROWSER) as client:
        sid = client.post(API, json={"payment": _payment(p)}, headers=_key()).json()["id"]
        with env.owner.begin() as conn:
            conn.execute(
                text("UPDATE delphi.guardian_sessions SET runner = 'WORKER' WHERE id = :s"),
                {"s": sid},
            )
        reply = client.post(f"{API}/{sid}/cancel")
    assert reply.status_code == 409 and reply.json()["code"] == "INVALID_STATE_TRANSITION"


# ---------------------------------------------------------------- the phone stream


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@dataclass
class Server:
    base: str
    origin: str
    live: LiveGuardian


@pytest.fixture
def server(env: Env) -> Iterator[Server]:
    live = _live(env, story_prior(KAMALA_PRIOR), KAMALA_PASSIVE)
    app = _app(env, _user(env, Role.INVESTIGATOR), live)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert srv.started
    origin = f"http://127.0.0.1:{port}"
    yield Server(f"{origin}/api/v1", origin, live)
    srv.should_exit = True
    thread.join(10)


class PhoneStream:
    """Reads one phone's SSE stream in a thread: each event as (type, data)."""

    def __init__(self, server: Server, customer: UUID) -> None:
        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.opened = threading.Event()
        self._stop = threading.Event()
        self._url = f"{server.base}/phone/stream"
        self._customer = str(customer)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        assert self.opened.wait(10), "the phone stream did not open"

    def _read(self) -> None:
        params = {"customer_id": self._customer}
        with httpx.stream("GET", self._url, params=params, timeout=WAIT_S) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            kind = ""
            for line in response.iter_lines():
                if line == ": open":
                    self.opened.set()
                elif line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    self.events.put((kind, json.loads(line[6:])))
                if self._stop.is_set():
                    return

    def next(self) -> tuple[str, dict[str, Any]]:
        return self.events.get(timeout=WAIT_S)

    def close(self) -> None:
        self._stop.set()


def _post(server: Server, path: str, body: dict[str, Any]) -> httpx.Response:
    headers = {"X-Delphi-Csrf": "1", "Origin": server.origin, **_key()}
    return httpx.post(f"{server.base}{path}", json=body, headers=headers, timeout=WAIT_S)


def test_the_phone_round_trip_over_its_stream(env: Env, server: Server) -> None:
    """Pay → probe.requested (an APPROVED variant only, FR-KB-05) → answer → the next probe →
    answer → session.decided; a phone that reconnects while a probe waits is shown it again."""
    p = _persona(env)
    phone = PhoneStream(server, p.customer)
    created = _post(server, "/guardian/sessions", {"payment": _payment(p)})
    assert created.status_code == 201, created.text
    sid = created.json()["id"]

    served: list[dict[str, Any]] = []
    while True:
        kind, data = phone.next()
        assert data["type"] == kind and data["session_id"] == sid
        if kind == "session.decided":
            decided = data
            break
        assert kind == "probe.requested"
        served.append(data)
        if len(served) == 1:  # a second phone opened now still gets the waiting probe
            late = PhoneStream(server, p.customer)
            assert late.next() == ("probe.requested", data)
            late.close()
        answer, latency, switches = KAMALA_ANSWERS[data["probe_key"]]
        reply = _post(
            server,
            f"/guardian/sessions/{sid}/probe-responses",
            {
                "probe_run_id": data["probe_run_id"],
                "answer": answer,
                "client_signals": _signals(env, latency, switches),
            },
        )
        assert reply.status_code == 200, reply.text
    phone.close()

    assert [s["probe_key"] for s in served[:2]] == [
        "PURPOSE_QUESTION",
        "SPELL_BREAKER_DIGITAL_ARREST",
    ]
    first = served[0]
    assert first["ui"] == "QUESTION" and first["timeout_s"] == 60
    assert [o["key"] for o in first["options"]][:1] == ["FAMILY_OR_FRIEND"]
    # FR-KB-05: Kamala reads Telugu, which has only unreviewed variants here, so she is served
    # an APPROVED English one (FR-GDN-09); never PENDING_REVIEW or REJECTED text.
    drafts = {vid for extra in UNREVIEWED.values() for vid, _, _ in extra}
    questions = [s for s in served if s["ui"] == "QUESTION"]
    assert questions and all(q["variant_id"] not in drafts for q in questions)
    assert all(q["language"] == "en" and UNREVIEWED_TEXT not in q["text"] for q in questions)
    assert decided["decision"] == "PROTECTIVE_HOLD" and decided["status"] == "DECIDED"


def test_the_stream_refuses_unknown_customers_and_bad_ids(env: Env, server: Server) -> None:
    unknown = httpx.get(
        f"{server.base}/phone/stream", params={"customer_id": str(uuid4())}, timeout=5
    )
    assert unknown.status_code == 404
    bad = httpx.get(f"{server.base}/phone/stream", params={"customer_id": "nope"}, timeout=5)
    assert bad.status_code == 422
    assert httpx.get(f"{server.base}/phone/stream", timeout=5).status_code == 422


def test_otp_events_reach_the_phone(env: Env) -> None:
    """The OTP protocol on the phone (docs/08 §4): the code as a mock SMS with the probe, a
    wrong code's attempts left, and a reconnecting phone shown the probe and its SMS again."""
    p = _persona(env)
    live = _live(env, FixedPrior(GENUINE_SURE, p_mule=None), CLEAN)
    with env.sessions.begin() as s:
        payment_id = bank.initiate_payment(
            s,
            bank.PaymentRequest(
                p.payer, 5_000 * INR, "UPI_PUSH", p.device, payee_account_id=p.payee
            ),
            env.clock.now(),
        )
    record = live.service.start(payment_id, mode=SessionMode.LIVE, runner=Runner.API)
    with env.sessions() as s:
        current = get_session(s, record.id)
    assert current is not None
    run_id = uuid4()
    issued = IssuedProbe(current, run_id, "OTP_STEP_UP", Ui.OTP, 60.0, otp_code="123456")

    async def scenario() -> list[list[tuple[str, dict[str, Any]]]]:
        hub = PhoneHub()
        channel = PhoneChannel(hub, env.sessions)
        first = hub.subscribe(p.customer)
        await channel.ask(issued)
        await channel.attempt_result(current, run_id, 2)
        late = hub.subscribe(p.customer)  # reconnects while the OTP waits
        await channel.finished(current)
        gone = hub.subscribe(p.customer)  # after the end: nothing waits
        out = []
        for sub in (first, late, gone):
            got = []
            while not sub.queue.empty():
                event = sub.queue.get_nowait()
                assert event is not None
                got.append((event.type, dict(event.data)))
            out.append(got)
        return out

    first, late, gone = asyncio.run(scenario())
    assert [t for t, _ in first] == [
        "probe.requested",
        "otp.sms",
        "probe.attempt_result",
        "session.decided",
    ]
    assert first[1][1]["code"] == "123456" and first[2][1]["attempts_remaining"] == 2
    assert first[0][1]["ui"] == "OTP" and first[0][1]["text"] is None
    assert [t for t, _ in late] == ["probe.requested", "otp.sms", "session.decided"]
    assert gone == []


# ---------------------------------------------------------------- a small pool under load (#284)

SMALL_POOL = 2
POOL_TIMEOUT_S = 5
IN_FLIGHT = 6


@dataclass
class DbPrior:
    """A prior that reads the bank on a connection of its own, as the sensing inference does
    (``SensingInference.prior_inputs``), from the same pool as the api."""

    engine: Any
    fixed: Any

    def prior_inputs(self, payment: bank.PaymentView) -> Any:
        with self.engine.connect() as conn:
            conn.execute(select(bank.payments.c.id).where(bank.payments.c.id == payment.id))
        return self.fixed.prior_inputs(payment)


@pytest.fixture
def small_pool(env: Env) -> Iterator[Env]:
    """The same database, its app role on a pool smaller than the requests in flight."""
    url = env.owner.url.render_as_string(hide_password=False)
    engine = create_engine(
        url,
        pool_size=SMALL_POOL,
        max_overflow=0,
        pool_timeout=POOL_TIMEOUT_S,
        connect_args={"options": "-c timezone=UTC -c role=delphi_app"},
    )
    yield Env(env.owner, make_session_factory(engine), env.clock)
    engine.dispose()


def test_concurrent_payments_never_starve_a_small_pool(small_pool: Env) -> None:
    """#284: neither the Idempotency-Key nor the session's steps hold a pooled connection while
    the prior takes another, so payments in flight beyond the pool's size all start."""
    env = small_pool
    persona = _persona(env)
    engine = env.sessions.kw["bind"]
    live = _live(env, DbPrior(engine, story_prior(KAMALA_PRIOR)), KAMALA_PASSIVE)
    app = _app(env, _user(env, Role.INVESTIGATOR), live)

    async def pay_all() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers=BROWSER
        ) as client:
            try:
                return await asyncio.gather(
                    *(
                        client.post(API, json={"payment": _payment(persona)}, headers=_key())
                        for _ in range(IN_FLIGHT)
                    )
                )
            finally:
                await live.runner.shutdown()  # the sessions' tasks end with this loop

    started = time.monotonic()
    replies = asyncio.run(pay_all())
    assert [r.status_code for r in replies] == [201] * IN_FLIGHT, [r.text for r in replies]
    assert time.monotonic() - started < POOL_TIMEOUT_S  # nobody waited out the pool
    # and none took the fail-safe (a prior that waited out the pool would have)
    assert not [r.json() for r in replies if r.json()["terminal_reason"] == "ENGINE_ERROR"]
