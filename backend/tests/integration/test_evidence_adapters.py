"""The Navigator evidence adapters on a real database (docs/09 §4, #102): each counts its rule's
facts over hand-built bank rows, relative to the case's ``as_of``, as the app role."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.loader import load_and_validate
from delphi.llm.port import Purpose
from delphi.llm.schemas import EvidenceNarrative
from delphi.llm.template import render_narrative
from delphi.llm.validators import validate
from delphi.navigator.evidence_adapters import ADAPTERS, EvidenceSubject, Fetched
from delphi.platform.db import make_session_factory
from delphi.probes.api import EVIDENCE_CLASSIFIERS
from delphi.probes.registry import CLASSIFIER_NAMES
from tests.conftest import KB_DIR

from ._db import engine_as, upgrade

AS_OF = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
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
        self,
        payer: UUID,
        payee: UUID,
        amount_inr: int,
        at: datetime,
        status: str = "EXECUTED",
        *,
        executed_at: datetime | None = None,
    ) -> UUID:
        """A payment initiated at ``at``; an EXECUTED one executes then unless told otherwise."""
        p = uuid4()
        done = executed_at or (at if status == "EXECUTED" else None)
        self.sql(
            "INSERT INTO bank.payments (id, payer_account_id, payee_account_id, amount_paise,"
            " channel, initiated_at, status, executed_at, origin)"
            " VALUES (:p, :a, :b, :m, 'IMPS', :t, :s, :e, 'SIM_HISTORY')",
            p=p,
            a=payer,
            b=payee,
            m=amount_inr * INR,
            t=at,
            s=status,
            e=done,
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
    after = bank.customer()  # a login after as_of: not counted
    bank.account(after)
    bank.login(
        after, bank.device(after, "fp-ring"), AS_OF + timedelta(hours=1), "Delhi", 28.6, 77.2
    )
    unopened = bank.customer()  # its only account was opened after as_of: not counted
    bank.account(unopened, age_days=-1)
    bank.login(
        unopened, bank.device(unopened, "fp-ring"), AS_OF - timedelta(days=1), "Delhi", 28, 77
    )
    outcome, fetched = _run(bank, "BENEFICIARY_DEVICE_SHARING", counterparty_account_id=subject)
    assert (outcome, fetched.facts["shared_accounts"]) == ("SHARED_3_PLUS", 3)
    assert len(fetched.record_refs) == 3
    edge = bank.customer()  # exactly 30 days before as_of: inside the window
    bank.account(edge)
    bank.login(edge, bank.device(edge, "fp-ring"), AS_OF - timedelta(days=30), "Delhi", 28.6, 77.2)
    _, fetched = _run(bank, "BENEFICIARY_DEVICE_SHARING", counterparty_account_id=subject)
    assert fetched.facts["shared_accounts"] == 4
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


def test_fan_in_counts_only_payments_that_credited_the_account_before_as_of(bank: Bank) -> None:
    """#193 review: the case's own held payment, declined attempts and later credits never
    count, and an old declined attempt is not a prior relation."""
    subject = bank.account(bank.customer())
    for _ in range(2):  # two genuinely new payers
        bank.pay(bank.account(bank.customer()), subject, 100, AS_OF - timedelta(hours=10))
    victim = bank.account(bank.customer())
    bank.pay(victim, subject, 50_000, AS_OF, status="HELD")  # the case's held payment
    bank.pay(bank.account(bank.customer()), subject, 100, AS_OF - timedelta(hours=2), "DECLINED")
    tried = bank.account(bank.customer())  # declined 10 days ago, credited now: still new
    bank.pay(tried, subject, 100, AS_OF - timedelta(days=10), status="DECLINED")
    bank.pay(tried, subject, 100, AS_OF - timedelta(hours=3))
    late = bank.account(bank.customer())  # initiated in the window, executed after as_of
    bank.pay(late, subject, 100, AS_OF - timedelta(hours=1), executed_at=AS_OF + timedelta(hours=1))
    at_as_of = bank.account(bank.customer())  # executed exactly at as_of: not before it
    bank.pay(at_as_of, subject, 100, AS_OF - timedelta(minutes=5), executed_at=AS_OF)
    edge = bank.account(bank.customer())  # executed exactly 48 h before as_of: in the window
    bank.pay(edge, subject, 100, AS_OF - timedelta(hours=48))
    outcome, fetched = _run(bank, "INBOUND_FAN_IN", counterparty_account_id=subject)
    assert fetched.facts["unrelated_payers"] == 4  # the two, `tried` and `edge`
    assert outcome == "MODERATE"
    lone = bank.account(bank.customer())
    bank.pay(bank.account(bank.customer()), lone, 100, AS_OF - timedelta(hours=1))
    bank.pay(bank.account(bank.customer()), lone, 100, AS_OF - timedelta(hours=1))
    bank.pay(victim, lone, 50_000, AS_OF, status="HELD")
    outcome, fetched = _run(bank, "INBOUND_FAN_IN", counterparty_account_id=lone)
    assert (outcome, fetched.facts["unrelated_payers"]) == ("LOW", 2)  # not MODERATE, 3


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
    assert fetched.facts["median_dwell_minutes"] == 12.0
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
    assert outcome == "OVER_24_H"  # the credit is still dwelling: unknown, never measured
    assert fetched.facts["median_dwell_minutes"] is None
    assert (fetched.facts["credits"], fetched.facts["still_dwelling"]) == (1, 1)
    assert _run(bank, "PASS_THROUGH_DWELL", counterparty_account_id=bank.account(None))[0] == (
        "OVER_24_H"
    )


def test_one_fresh_unmoved_credit_is_not_a_fast_pass_through(bank: Bank) -> None:
    """#191 review: ₹50,000 credited 10 minutes before as_of, none of it moved."""
    subject = bank.account(bank.customer())
    credit = bank.pay(bank.account(bank.customer()), subject, 50_000, AS_OF - timedelta(minutes=10))
    bank.ledger(subject, credit, 50_000, 50_000, AS_OF - timedelta(minutes=10))
    outcome, fetched = _run(bank, "PASS_THROUGH_DWELL", counterparty_account_id=subject)
    assert outcome == "OVER_24_H" and outcome != "UNDER_30_MIN"
    assert fetched.facts["median_dwell_minutes"] is None
    assert fetched.facts["still_dwelling"] == 1


def test_dwell_reads_the_ledger_from_7_days_before_as_of_up_to_it(bank: Bank) -> None:
    subject = bank.account(bank.customer())
    payer, onward = bank.account(bank.customer()), bank.account(bank.customer())
    t0 = AS_OF - timedelta(days=7)  # exactly 7 days: in the window
    credit = bank.pay(payer, subject, 1_000, t0)
    bank.ledger(subject, credit, 1_000, 1_000, t0)
    out = bank.pay(subject, onward, 900, t0 + timedelta(minutes=20))
    bank.ledger(subject, out, -900, 100, t0 + timedelta(minutes=20))
    later = bank.pay(payer, subject, 1_000, AS_OF + timedelta(minutes=1))  # after as_of: ignored
    bank.ledger(subject, later, 1_000, 1_100, AS_OF + timedelta(minutes=1))
    outcome, fetched = _run(bank, "PASS_THROUGH_DWELL", counterparty_account_id=subject)
    assert (outcome, fetched.facts["credits"]) == ("UNDER_30_MIN", 1)
    assert fetched.facts["median_dwell_minutes"] == 20.0


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


def test_an_account_the_bank_does_not_hold_is_not_a_new_account(bank: Bank) -> None:
    """#193 review: an unknown age is not evidence of a new account."""
    outcome, fetched = _run(bank, "ACCOUNT_PROFILE", counterparty_account_id=uuid4())
    assert outcome == "ESTABLISHED"
    assert fetched.facts == {"account_age_days": None, "kyc_level": None}


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
    for kind, days in (("SIM_SWAP_REPORTED", 14.01), ("DEVICE_LOST_REPORTED", -1)):  # old, later
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
    assert outcome == "SIM_SWAP_OR_DEVICE_LOSS_REPORTED"
    assert (fetched.facts["event"], fetched.facts["days_ago"]) == ("DEVICE_LOST_REPORTED", 13)
    edge = bank.customer()  # exactly 14 days before as_of: inside the look-back
    bank.sql(
        "INSERT INTO bank.crm_events (id, customer_id, type, at) VALUES (:i, :c, :k, :t)",
        i=uuid4(),
        c=edge,
        k="SIM_SWAP_REPORTED",
        t=AS_OF - timedelta(days=14),
    )
    assert _run(bank, "CRM_CONTACT_HISTORY", payer_customer_id=edge)[0] == (
        "SIM_SWAP_OR_DEVICE_LOSS_REPORTED"
    )


def test_login_geo_velocity(bank: Bank) -> None:
    payer = bank.customer()
    phone = bank.device(payer, "fp-own")
    bank.login(payer, phone, AS_OF - timedelta(days=30), "Delhi", 28.6139, 77.2090)
    bank.login(payer, phone, AS_OF - timedelta(hours=3), "Delhi", 28.6139, 77.2090)
    assert _run(bank, "LOGIN_GEO_VELOCITY", payer_customer_id=payer)[0] == "USUAL"
    # Delhi → Mumbai (~1,150 km) in 30 minutes: impossible.
    bank.login(payer, phone, AS_OF - timedelta(hours=2, minutes=30), "Mumbai", 19.0760, 72.8777)
    outcome, fetched = _run(bank, "LOGIN_GEO_VELOCITY", payer_customer_id=payer)
    assert outcome == "IMPOSSIBLE_TRAVEL" and fetched.facts["speed_kmh"] > 2000


def test_geo_velocity_uses_the_window_and_ignores_logins_after_as_of(bank: Bank) -> None:
    payer = bank.customer()
    phone = bank.device(payer, "fp-own")
    # Mumbai exactly 24 h before as_of (in the window), Delhi 30 min later: impossible.
    bank.login(payer, phone, AS_OF - timedelta(hours=24), "Mumbai", 19.0760, 72.8777)
    bank.login(payer, phone, AS_OF - timedelta(hours=23, minutes=30), "Delhi", 28.6139, 77.2090)
    assert _run(bank, "LOGIN_GEO_VELOCITY", payer_customer_id=payer)[0] == "IMPOSSIBLE_TRAVEL"
    other = bank.customer()
    tablet = bank.device(other, "fp-tab")
    bank.login(other, tablet, AS_OF - timedelta(days=9), "Delhi", 28.6139, 77.2090)
    bank.login(other, tablet, AS_OF - timedelta(days=5), "Delhi", 28.6139, 77.2090)
    # A login after as_of from a new, far city is not seen.
    bank.login(other, tablet, AS_OF + timedelta(minutes=5), "Chennai", 13.0827, 80.2707)
    outcome, fetched = _run(bank, "LOGIN_GEO_VELOCITY", payer_customer_id=other)
    assert outcome == "USUAL"
    # The last login at or before as_of is days old: it still names the city.
    assert fetched.facts["last_city"] == "Delhi" and fetched.facts["new_city"] is False


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
    # Initiated before as_of but executed after it: not yet a prior payment (#193 review).
    bank.pay(
        payer,
        payee,
        80_000,
        AS_OF - timedelta(hours=1),
        executed_at=AS_OF + timedelta(hours=2),
    )
    assert _run(bank, "PAYEE_RELATIONSHIP", **subject)[0] == "PRIOR_SMALL_PAYMENTS"
    bank.pay(payer, payee, 5_000, AS_OF - timedelta(days=7))
    outcome, fetched = _run(bank, "PAYEE_RELATIONSHIP", **subject)
    assert outcome == "ESTABLISHED_PAYEE" and fetched.facts["prior_payments"] == 2


NEUTRAL = {
    "BENEFICIARY_DEVICE_SHARING": "NOT_SHARED",
    "INBOUND_FAN_IN": "LOW",
    "PASS_THROUGH_DWELL": "OVER_24_H",
    "ACCOUNT_PROFILE": "ESTABLISHED",
    "LINKED_COMPLAINTS": "NONE",
    "CRM_CONTACT_HISTORY": "NONE",
    "LOGIN_GEO_VELOCITY": "USUAL",
    "PAYEE_RELATIONSHIP": "NO_PRIOR_RELATION",
}


@pytest.mark.parametrize("key", sorted(NEUTRAL))
def test_with_no_subject_every_adapter_gives_its_neutral_outcome(bank: Bank, key: str) -> None:
    """No counterparty, payer or customer: the neutral outcome, and facts the template renders."""
    outcome, fetched = _run(bank, key)
    assert outcome == NEUTRAL[key]
    assert set(fetched.facts) == FACTS_CONTRACT[key]
    _assert_renders(key, fetched)


# docs/15 §7: the narrative payload's facts, per source, exactly.
FACTS_CONTRACT = {
    "BENEFICIARY_DEVICE_SHARING": {"shared_accounts", "window_days"},
    "INBOUND_FAN_IN": {"unrelated_payers", "window_h"},
    "PASS_THROUGH_DWELL": {
        "median_dwell_minutes",
        "credits",
        "still_dwelling",
        "window_days",
        "departure_share",
    },
    "ACCOUNT_PROFILE": {"account_age_days", "kyc_level"},
    "LINKED_COMPLAINTS": {"complaints"},
    "CRM_CONTACT_HISTORY": {"event", "days_ago", "window_days"},
    "LOGIN_GEO_VELOCITY": {"last_city", "speed_kmh", "new_city", "window_h"},
    "PAYEE_RELATIONSHIP": {"prior_payments", "max_prior_amount_paise"},
}


def test_every_adapter_returns_the_narrative_facts_contract(bank: Bank) -> None:
    """docs/15 §7: each adapter's facts are exactly its source's narrative fields, and the
    source's template renders them into text that passes the docs/15 §5 validators."""
    payer_customer = bank.customer()
    payer = bank.account(payer_customer)
    payee = bank.account(bank.customer(), age_days=20, kyc="MINIMAL")
    phone = bank.device(payer_customer, "fp")
    bank.login(payer_customer, phone, AS_OF - timedelta(hours=3), "Delhi", 28.6, 77.2)
    paid = bank.pay(payer, payee, 6_000, AS_OF - timedelta(days=3))
    bank.ledger(payee, paid, 6_000, 6_000, AS_OF - timedelta(days=3))
    subjects: dict[str, Any] = {
        "counterparty_account_id": payee,
        "payer_account_id": payer,
        "payer_customer_id": payer_customer,
    }
    assert set(FACTS_CONTRACT) == set(ADAPTERS)
    for key in ADAPTERS:
        _, fetched = _run(bank, key, **subjects)
        assert set(fetched.facts) == FACTS_CONTRACT[key], key
        _assert_renders(key, fetched)


def _assert_renders(key: str, fetched: Any) -> None:
    payload = {
        "source_key": key,
        "record_id": "EV-1",
        "subject": "ACC-…5678",
        "as_of": AS_OF.isoformat(),
        "facts": dict(fetched.facts),
    }
    text = render_narrative(payload)
    verdict = validate(
        Purpose.EVIDENCE_NARRATIVE, EvidenceNarrative(finding=text, citations=["EV-1"]), payload
    )
    assert verdict.passed, (key, text, verdict.failures)


@pytest.mark.parametrize(
    "facts",
    [
        {"median_dwell_minutes": None, "credits": 3, "still_dwelling": 2},  # median unknown
        {"median_dwell_minutes": 12.0, "credits": 3, "still_dwelling": 1},  # measured decides
    ],
)
def test_the_dwell_narrative_says_when_credits_are_still_dwelling(facts: dict[str, Any]) -> None:
    fetched = Fetched(None, {**facts, "window_days": 7, "departure_share": 0.8})
    _assert_renders("PASS_THROUGH_DWELL", fetched)
