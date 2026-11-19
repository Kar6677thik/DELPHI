"""The Navigator evidence adapters on a real database (docs/09 §4, #102): each counts its rule's
facts over hand-built bank rows, relative to the case's ``as_of``, as the app role."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.navigator.evidence_adapters import ADAPTERS, EvidenceSubject
from delphi.platform.db import make_session_factory
from delphi.probes.evidence import EVIDENCE_CLASSIFIERS
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sim import bank as bank_tables
from delphi.sim import truth
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import load_generator_config
from delphi.sim.writer import write_dataset
from tests.conftest import KB_DIR

from ._db import engine_as, upgrade

AS_OF = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
DEMO_CREATED_AT = datetime(2026, 11, 1, tzinfo=UTC)
NFR_04_SECONDS = 2.0  # #102: evidence fetch p95 on the demo dataset
T = load_and_validate(KB_DIR, CLASSIFIER_NAMES).config.classifiers
INR = 100


@dataclass
class Bank:
    owner: Engine
    sessions: sessionmaker[Session]

    def sql(self, statement: str, **params: Any) -> None:
        with self.owner.begin() as conn:
            conn.execute(text(statement), params)

    def customer(self, city: str = "Delhi") -> UUID:
        c = uuid4()
        self.sql(
            "INSERT INTO bank.customers (id, display_name, age, language, is_vulnerable, segment,"
            " income_band, consent_call_status, home_city)"
            " VALUES (:c, 'C', 40, 'en', false, 'RETAIL', 'MID', true, :city)",
            c=c,
            city=city,
        )
        return c

    def account(
        self, customer: UUID | None = None, *, age_days: float = 900, kyc: str = "FULL"
    ) -> UUID:
        a = uuid4()
        self.sql(
            "INSERT INTO bank.accounts (id, customer_id, bank_code, opened_at, kyc_level, status,"
            " balance_paise) VALUES (:a, :c, 'DLPH', :t, :k, 'ACTIVE', 0)",
            a=a,
            c=customer,
            t=AS_OF - timedelta(days=age_days),
            k=kyc,
        )
        return a

    def device(self, customer: UUID, fingerprint: str) -> UUID:
        d = uuid4()
        self.sql(
            "INSERT INTO bank.devices (id, customer_id, fingerprint, os, first_seen_at)"
            " VALUES (:d, :c, :f, 'ANDROID', :t)",
            d=d,
            c=customer,
            f=fingerprint,
            t=AS_OF - timedelta(days=400),
        )
        return d

    def login(
        self, customer: UUID, device: UUID, at: datetime, city: str, lat: float, lon: float
    ) -> None:
        self.sql(
            "INSERT INTO bank.login_events (id, customer_id, device_id, ip_prefix24, lat, lon,"
            " city, at) VALUES (:i, :c, :d, '10.0.0.0/24', :lat, :lon, :city, :at)",
            i=uuid4(),
            c=customer,
            d=device,
            lat=lat,
            lon=lon,
            city=city,
            at=at,
        )

    def pay(
        self, payer: UUID, payee: UUID, amount_inr: int, at: datetime, status: str = "EXECUTED"
    ) -> UUID:
        p = uuid4()
        self.sql(
            "INSERT INTO bank.payments (id, payer_account_id, payee_account_id, amount_paise,"
            " channel, initiated_at, status, origin)"
            " VALUES (:p, :a, :b, :m, 'IMPS', :t, :s, 'SIM_HISTORY')",
            p=p,
            a=payer,
            b=payee,
            m=amount_inr * INR,
            t=at,
            s=status,
        )
        return p

    def ledger(
        self, account: UUID, payment: UUID, delta_inr: int, after_inr: int, at: datetime
    ) -> None:
        self.sql(
            "INSERT INTO bank.ledger_entries (account_id, payment_id, delta_paise,"
            " balance_after_paise, at) VALUES (:a, :p, :d, :b, :t)",
            a=account,
            p=payment,
            d=delta_inr * INR,
            b=after_inr * INR,
            t=at,
        )


@pytest.fixture
def bank(create_database: Callable[[], str]) -> Iterator[Bank]:
    url = create_database()
    upgrade(url)
    owner = engine_as(url)
    app = engine_as(url, "delphi_app")
    yield Bank(owner, make_session_factory(app))
    app.dispose()
    owner.dispose()


def _run(bank: Bank, key: str, **subject: Any) -> tuple[str, Any]:
    full = {
        "as_of": AS_OF,
        "now": AS_OF + timedelta(hours=1),
        "counterparty_account_id": None,
        "payer_account_id": None,
        "payer_customer_id": None,
        **subject,
    }
    with bank.sessions() as s:
        fetched = ADAPTERS[key](s, EvidenceSubject(**full), T)
    return EVIDENCE_CLASSIFIERS[key].classify(fetched.observation, T), fetched


def test_every_ruled_source_has_an_adapter() -> None:
    assert set(ADAPTERS) == set(EVIDENCE_CLASSIFIERS)


def test_device_sharing_counts_other_owners_accounts_on_the_same_fingerprint(bank: Bank) -> None:
    mule = bank.customer()
    subject = bank.account(mule)
    bank.login(mule, bank.device(mule, "fp-ring"), AS_OF - timedelta(days=2), "Delhi", 28.6, 77.2)
    for _ in range(3):  # three others on a device with the same fingerprint, in the window
        other = bank.customer()
        bank.account(other)
        bank.login(
            other, bank.device(other, "fp-ring"), AS_OF - timedelta(days=5), "Delhi", 28.6, 77.2
        )
    late = bank.customer()  # outside the 30-day window: not counted
    bank.account(late)
    bank.login(late, bank.device(late, "fp-ring"), AS_OF - timedelta(days=31), "Delhi", 28.6, 77.2)
    outcome, fetched = _run(bank, "BENEFICIARY_DEVICE_SHARING", counterparty_account_id=subject)
    assert (outcome, fetched.facts["other_accounts"]) == ("SHARED_3_PLUS", 3)
    assert len(fetched.record_refs) == 3
    external = bank.account(None)
    assert _run(bank, "BENEFICIARY_DEVICE_SHARING", counterparty_account_id=external)[0] == (
        "NOT_SHARED"
    )


def test_fan_in_counts_new_payers_in_48_hours(bank: Bank) -> None:
    subject = bank.account(bank.customer())
    known = bank.account(bank.customer())
    bank.pay(known, subject, 100, AS_OF - timedelta(days=10))  # a prior relation
    bank.pay(known, subject, 100, AS_OF - timedelta(hours=5))
    for _ in range(3):
        bank.pay(bank.account(bank.customer()), subject, 100, AS_OF - timedelta(hours=40))
    bank.pay(bank.account(bank.customer()), subject, 100, AS_OF - timedelta(hours=49))  # too old
    outcome, fetched = _run(bank, "INBOUND_FAN_IN", counterparty_account_id=subject)
    assert (outcome, fetched.facts["unrelated_payers"]) == ("MODERATE", 3)


def test_dwell_follows_each_credit_out_fifo(bank: Bank) -> None:
    subject = bank.account(bank.customer())
    payer, onward = bank.account(bank.customer()), bank.account(bank.customer())
    t0 = AS_OF - timedelta(days=2)
    credit = bank.pay(payer, subject, 10_000, t0)
    bank.ledger(subject, credit, 10_000, 10_000, t0)  # no money before: the credit heads the queue
    out1 = bank.pay(subject, onward, 5_000, t0 + timedelta(minutes=5))
    bank.ledger(subject, out1, -5_000, 5_000, t0 + timedelta(minutes=5))
    out2 = bank.pay(subject, onward, 3_500, t0 + timedelta(minutes=12))
    bank.ledger(subject, out2, -3_500, 1_500, t0 + timedelta(minutes=12))  # 85% gone at 12 min
    outcome, fetched = _run(bank, "PASS_THROUGH_DWELL", counterparty_account_id=subject)
    assert outcome == "UNDER_30_MIN"
    assert fetched.facts["minutes_to_80pct_out"] == [12.0]
    assert fetched.record_refs  # the credit's ledger entry


def test_dwell_counts_an_older_balance_ahead_of_the_credit(bank: Bank) -> None:
    """FIFO: money already in the account leaves first, so the credit itself dwells longer."""
    subject = bank.account(bank.customer())
    payer, onward = bank.account(bank.customer()), bank.account(bank.customer())
    t0 = AS_OF - timedelta(days=2)
    credit = bank.pay(payer, subject, 10_000, t0)
    bank.ledger(subject, credit, 10_000, 30_000, t0)  # ₹20,000 was there before
    out = bank.pay(subject, onward, 25_000, t0 + timedelta(minutes=10))
    bank.ledger(subject, out, -25_000, 5_000, t0 + timedelta(minutes=10))  # only 50% of the credit
    outcome, fetched = _run(bank, "PASS_THROUGH_DWELL", counterparty_account_id=subject)
    assert outcome == "OVER_24_H" and fetched.facts["still_dwelling"] == 1
    assert _run(bank, "PASS_THROUGH_DWELL", counterparty_account_id=bank.account(None))[0] == (
        "OVER_24_H"
    )


@pytest.mark.parametrize(
    ("age", "kyc", "expected"),
    [
        (30, "MINIMAL", "NEW_MINIMAL_KYC"),
        (30, "FULL", "NEW_FULL_KYC"),
        (400, "MINIMAL", "ESTABLISHED"),
    ],
)
def test_account_profile(bank: Bank, age: int, kyc: str, expected: str) -> None:
    subject = bank.account(bank.customer(), age_days=age, kyc=kyc)
    outcome, fetched = _run(bank, "ACCOUNT_PROFILE", counterparty_account_id=subject)
    assert outcome == expected and fetched.facts["kyc_level"] == kyc


def test_linked_complaints_counts_those_known_by_now(bank: Bank) -> None:
    subject = bank.account(None)
    victim = bank.customer()
    for reported in (AS_OF - timedelta(days=1), AS_OF + timedelta(days=3)):  # the second: later
        bank.sql(
            "INSERT INTO bank.complaints (id, reported_at, victim_customer_id, channel, category,"
            " payment_ids, counterparty_account_ids)"
            " VALUES (:i, :t, :v, 'NCRP_1930', 'DIGITAL_ARREST_SCAM', '{}', ARRAY[:a]::uuid[])",
            i=uuid4(),
            t=reported,
            v=victim,
            a=subject,
        )
    outcome, fetched = _run(bank, "LINKED_COMPLAINTS", counterparty_account_id=subject)
    assert (outcome, fetched.facts["complaints"]) == ("ONE_OR_MORE", 1)


def test_crm_contact_history_looks_back_14_days(bank: Bank) -> None:
    payer = bank.customer()
    for kind, days in (("SIM_SWAP_REPORTED", 15), ("DEVICE_LOST_REPORTED", -1)):  # too old, later
        bank.sql(
            "INSERT INTO bank.crm_events (id, customer_id, type, at) VALUES (:i, :c, :k, :t)",
            i=uuid4(),
            c=payer,
            k=kind,
            t=AS_OF - timedelta(days=days),
        )
    assert _run(bank, "CRM_CONTACT_HISTORY", payer_customer_id=payer)[0] == "NONE"
    bank.sql(
        "INSERT INTO bank.crm_events (id, customer_id, type, at) VALUES (:i, :c, :k, :t)",
        i=uuid4(),
        c=payer,
        k="DEVICE_LOST_REPORTED",
        t=AS_OF - timedelta(days=13),
    )
    outcome, fetched = _run(bank, "CRM_CONTACT_HISTORY", payer_customer_id=payer)
    assert outcome == "SIM_SWAP_OR_DEVICE_LOSS_REPORTED" and fetched.facts["reports"] == 1


def test_login_geo_velocity(bank: Bank) -> None:
    payer = bank.customer()
    phone = bank.device(payer, "fp-own")
    bank.login(payer, phone, AS_OF - timedelta(days=30), "Delhi", 28.6139, 77.2090)
    bank.login(payer, phone, AS_OF - timedelta(hours=3), "Delhi", 28.6139, 77.2090)
    assert _run(bank, "LOGIN_GEO_VELOCITY", payer_customer_id=payer)[0] == "USUAL"
    # Delhi → Mumbai (~1,150 km) in 30 minutes: impossible.
    bank.login(payer, phone, AS_OF - timedelta(hours=2, minutes=30), "Mumbai", 19.0760, 72.8777)
    outcome, fetched = _run(bank, "LOGIN_GEO_VELOCITY", payer_customer_id=payer)
    assert outcome == "IMPOSSIBLE_TRAVEL" and fetched.facts["max_speed_kmh"] > 2000


def test_a_new_city_without_impossible_travel(bank: Bank) -> None:
    payer = bank.customer()
    phone = bank.device(payer, "fp-own")
    bank.login(payer, phone, AS_OF - timedelta(days=3), "Delhi", 28.6139, 77.2090)
    bank.login(payer, phone, AS_OF - timedelta(hours=1), "Jaipur", 26.9124, 75.7873)
    outcome, fetched = _run(bank, "LOGIN_GEO_VELOCITY", payer_customer_id=payer)
    assert outcome == "NEW_LOCATION" and fetched.facts["last_city"] == "Jaipur"
    first_ever = bank.customer()
    bank.login(
        first_ever, bank.device(first_ever, "fp-2"), AS_OF - timedelta(hours=1), "Pune", 18.5, 73.8
    )
    assert _run(bank, "LOGIN_GEO_VELOCITY", payer_customer_id=first_ever)[0] == "USUAL"


def test_payee_relationship_counts_earlier_executed_payments(bank: Bank) -> None:
    payer, payee = bank.account(bank.customer()), bank.account(None)
    subject = {"payer_account_id": payer, "counterparty_account_id": payee}
    assert _run(bank, "PAYEE_RELATIONSHIP", **subject)[0] == "NO_PRIOR_RELATION"
    bank.pay(payer, payee, 4_999, AS_OF - timedelta(days=9))
    bank.pay(payer, payee, 90_000, AS_OF - timedelta(days=8), status="DECLINED")  # not executed
    assert _run(bank, "PAYEE_RELATIONSHIP", **subject)[0] == "PRIOR_SMALL_PAYMENTS"
    bank.pay(payer, payee, 5_000, AS_OF - timedelta(days=7))
    outcome, fetched = _run(bank, "PAYEE_RELATIONSHIP", **subject)
    assert outcome == "ESTABLISHED_PAYEE" and fetched.facts["prior_payments"] == 2


@pytest.mark.skipif(
    os.environ.get("DELPHI_RUN_SLOW") != "1", reason="slow: set DELPHI_RUN_SLOW=1 to run"
)
def test_nfr_04_evidence_fetch_on_the_demo_dataset(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    """NFR-04 / #102: an evidence fetch (adapter + rule) takes 2 s or less at p95 on `demo`,
    over every source for the 25 largest fraud payments (their payees are mule accounts)."""
    url = create_database()
    upgrade(url)
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:
        write_dataset(
            conn,
            generate_dataset(load_generator_config(), "demo", kb=kb, created_at=DEMO_CREATED_AT),
        )
    with sim.connect() as conn:  # the test picks frauds from the truth; adapters see the bank
        p, pt = bank_tables.payments, truth.payment_truth
        frauds = conn.execute(
            select(p.c.payer_account_id, p.c.payee_account_id, p.c.initiated_at)
            .join(pt, pt.c.payment_id == p.c.id)
            .where(pt.c.is_fraud)
            .order_by(p.c.amount_paise.desc(), p.c.id)
            .limit(25)
        ).all()
        owners = {
            r.id: r.customer_id
            for r in conn.execute(
                select(bank_tables.accounts.c.id, bank_tables.accounts.c.customer_id)
            )
        }
    sim.dispose()
    app = make_session_factory(engine_as(url, "delphi_app"))
    timings: list[float] = []
    with app() as s:
        for payer, payee, at in frauds:
            subject = EvidenceSubject(at, at, payee, payer, owners.get(payer))
            for key, adapter in ADAPTERS.items():
                started = time.perf_counter()
                EVIDENCE_CLASSIFIERS[key].classify(adapter(s, subject, T).observation, T)
                timings.append(time.perf_counter() - started)
    p95 = sorted(timings)[int(0.95 * (len(timings) - 1))]
    print(  # the timings are the PR's evidence
        f"demo evidence fetch: {len(timings)} fetches, p95 {p95 * 1000:.1f} ms, "
        f"max {max(timings) * 1000:.1f} ms"
    )
    assert p95 <= NFR_04_SECONDS
