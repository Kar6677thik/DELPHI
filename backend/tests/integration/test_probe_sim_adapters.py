"""The simulated passive-probe adapters on real PostgreSQL, as `delphi_app` (docs/04 §4.2,
docs/08 §5): they read only the `bank` schema, only data from before `as_of`, and their queries
carry a statement_timeout."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, insert, text

from delphi.kb.model import KnowledgeBase
from delphi.probes.passive import (
    PassiveRequest,
    PassiveStatus,
    passive_executor,
    passive_fetchers,
    run_passive_probes,
)
from delphi.probes.sim_adapters import SimBeneficiaryIntel, SimPhoneRisk, SimSessionTelemetry
from delphi.sim import api as sim

from ._db import engine_as, upgrade

AS_OF = datetime(2026, 11, 9, 18, 10, tzinfo=UTC)
TIMEOUT_MS = 2_000


@pytest.fixture(scope="module")
def db_url(create_database: Callable[[], str]) -> str:
    url = create_database()
    upgrade(url)
    return url


@pytest.fixture(scope="module")
def owner(db_url: str) -> Iterator[Engine]:
    engine = engine_as(db_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def app(db_url: str) -> Iterator[Engine]:
    engine = engine_as(db_url, "delphi_app")
    yield engine
    engine.dispose()


class FixedMuleScore:
    def __init__(self, score: float) -> None:
        self.score = score
        self.calls: list[tuple[UUID, datetime]] = []

    def p_mule(self, account_id: UUID, as_of: datetime) -> float:
        self.calls.append((account_id, as_of))
        return self.score


@dataclass
class Bank:
    """Inserts minimal valid bank rows through the owner engine."""

    engine: Engine

    def _insert(self, table: Any, **values: Any) -> Any:
        if "id" in table.c:
            values.setdefault("id", uuid.uuid4())
        if "created_at" in table.c:
            values.setdefault("created_at", AS_OF - timedelta(days=500))
        with self.engine.begin() as conn:
            conn.execute(insert(table).values(**values))
        return values.get("id")

    def customer(self) -> UUID:
        return self._insert(
            sim.customers,
            display_name="Test",
            age=40,
            language="en",
            is_vulnerable=False,
            disability_flag=False,
            segment="RETAIL",
            income_band="MID",
            consent_call_status=True,
            home_city="Hyderabad",
        )

    def account(self, customer: UUID | None = None, **values: Any) -> UUID:
        values.setdefault("is_merchant", False)
        return self._insert(
            sim.accounts,
            customer_id=customer,
            bank_code="DLPH",
            opened_at=AS_OF - timedelta(days=400),
            kyc_level="FULL",
            status="ACTIVE",
            balance_paise=10_000_000,
            version=1,
            **values,
        )

    def device(self, customer: UUID, first_seen_at: datetime) -> UUID:
        return self._insert(
            sim.devices,
            customer_id=customer,
            fingerprint=uuid.uuid4().hex,
            os="ANDROID",
            first_seen_at=first_seen_at,
        )

    def payment(
        self,
        payer: UUID,
        payee: UUID,
        at: datetime,
        status: str = "EXECUTED",
        device: UUID | None = None,
    ) -> UUID:
        return self._insert(
            sim.payments,
            payer_account_id=payer,
            payee_account_id=payee,
            amount_paise=100_000,
            channel="UPI_PUSH",
            initiated_at=at,
            device_id=device,
            status=status,
            origin="SIM_HISTORY",
        )

    def telemetry(self, payment: UUID, **values: Any) -> None:
        row = {
            "typing_cadence_z": 0.2,
            "paste_on_account_field": False,
            "max_field_dwell_s": 2.0,
            "remote_access_active": False,
            "screen_share_active": False,
            "on_call": False,
            "call_counterparty_known": None,
            "session_duration_s": 40.0,
        } | values
        self._insert(sim.session_telemetry, payment_id=payment, **row)


@pytest.fixture(scope="module")
def bank(owner: Engine) -> Bank:
    return Bank(owner)


def _request(payer_customer: UUID, payee: UUID, payment: UUID, device: UUID | None):
    return PassiveRequest(payment, payer_customer, payee, device, AS_OF)


def _payer(bank: Bank) -> tuple[UUID, UUID]:
    customer = bank.customer()
    return customer, bank.account(customer)


# ---------------------------------------------------------------- BENEFICIARY_INTEL


def test_beneficiary_history_counts_only_approved_payments_before_as_of(
    bank: Bank, app: Engine
) -> None:
    payee = bank.account(bank.customer())
    payers = [_payer(bank) for _ in range(3)]
    first = AS_OF - timedelta(days=120)
    for i, (_, account) in enumerate(payers):
        bank.payment(account, payee, first + timedelta(days=i))
    _, other = payers[0]
    bank.payment(other, payee, AS_OF - timedelta(days=1), status="DECLINED")  # not counted
    bank.payment(other, payee, AS_OF - timedelta(days=1), status="HELD")  # not counted
    bank.payment(other, payee, AS_OF + timedelta(minutes=1))  # after as_of: not counted

    scores = FixedMuleScore(0.1)
    obs = SimBeneficiaryIntel(app, scores, TIMEOUT_MS).beneficiary_intel(
        _request(payers[0][0], payee, uuid.uuid4(), None)
    )
    assert (obs.earlier_payments, obs.earlier_distinct_customers) == (3, 3)
    assert obs.first_payment_at == first
    assert not obs.listed_in_suspect_registry
    assert not obs.is_registered_merchant
    assert obs.p_mule == 0.1
    assert scores.calls == [(payee, AS_OF)]


def test_beneficiary_registry_listing_counts_only_once_listed(bank: Bank, app: Engine) -> None:
    listed_before = bank.account(bank.customer())
    listed_after = bank.account(bank.customer())
    bank._insert(
        sim.sim_registry_listings, account_id=listed_before, listed_at=AS_OF - timedelta(hours=1)
    )
    bank._insert(
        sim.sim_registry_listings, account_id=listed_after, listed_at=AS_OF + timedelta(hours=1)
    )
    adapter = SimBeneficiaryIntel(app, FixedMuleScore(0.0), TIMEOUT_MS)
    customer, _ = _payer(bank)
    assert adapter.beneficiary_intel(
        _request(customer, listed_before, uuid.uuid4(), None)
    ).listed_in_suspect_registry
    assert not adapter.beneficiary_intel(
        _request(customer, listed_after, uuid.uuid4(), None)
    ).listed_in_suspect_registry


def test_beneficiary_merchant_flag(bank: Bank, app: Engine) -> None:
    merchant = bank.account(None, is_merchant=True, merchant_category="HOSPITAL")
    customer, _ = _payer(bank)
    obs = SimBeneficiaryIntel(app, FixedMuleScore(0.0), TIMEOUT_MS).beneficiary_intel(
        _request(customer, merchant, uuid.uuid4(), None)
    )
    assert obs.is_registered_merchant
    assert obs.earlier_payments == 0 and obs.first_payment_at is None


def test_beneficiary_unknown_payee_raises(app: Engine) -> None:
    with pytest.raises(LookupError, match="payee account"):
        SimBeneficiaryIntel(app, FixedMuleScore(0.0), TIMEOUT_MS).beneficiary_intel(
            _request(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), None)
        )


# ---------------------------------------------------------------- COUNTERPARTY_PHONE_RISK


@pytest.mark.parametrize("category", ["VERY_HIGH", "MEDIUM", None])
def test_phone_risk_reads_the_payee_phones_fri_category(
    bank: Bank, app: Engine, category: str | None
) -> None:
    phone = f"+9199{uuid.uuid4().int % 10**8:08d}"
    payee = bank.account(bank.customer(), phone_number=phone)
    if category is not None:
        bank._insert(sim.sim_phone_risk, phone_number=phone, category=category)
    customer, _ = _payer(bank)
    obs = SimPhoneRisk(app, TIMEOUT_MS).phone_risk(_request(customer, payee, uuid.uuid4(), None))
    assert obs.fri_category == category


def test_phone_risk_payee_without_phone(bank: Bank, app: Engine) -> None:
    payee = bank.account(bank.customer())
    customer, _ = _payer(bank)
    obs = SimPhoneRisk(app, TIMEOUT_MS).phone_risk(_request(customer, payee, uuid.uuid4(), None))
    assert obs.fri_category is None


# ---------------------------------------------------------------- telemetry probes


@pytest.fixture
def session(bank: Bank) -> Callable[..., PassiveRequest]:
    def make(device_age: timedelta = timedelta(days=400), **telemetry: Any) -> PassiveRequest:
        customer, account = _payer(bank)
        payee = bank.account(bank.customer())
        device = bank.device(customer, AS_OF - device_age)
        payment = bank.payment(account, payee, AS_OF, status="PENDING_GUARDIAN", device=device)
        bank.telemetry(payment, **telemetry)
        return _request(customer, payee, payment, device)

    return make


def test_device_posture(session: Callable[..., PassiveRequest], app: Engine) -> None:
    adapter = SimSessionTelemetry(app, TIMEOUT_MS)
    obs = adapter.device_posture(session(device_age=timedelta(days=2), screen_share_active=True))
    assert obs.screen_share_active and not obs.remote_access_active
    assert obs.device_first_seen_at == AS_OF - timedelta(days=2)
    assert obs.as_of == AS_OF


def test_session_behaviour(session: Callable[..., PassiveRequest], app: Engine) -> None:
    obs = SimSessionTelemetry(app, TIMEOUT_MS).session_behaviour(
        session(paste_on_account_field=True, max_field_dwell_s=25.0, typing_cadence_z=3.5)
    )
    assert (obs.paste_on_account_field, obs.max_field_dwell_s, obs.typing_cadence_z) == (
        True,
        25.0,
        3.5,
    )


@pytest.mark.parametrize(
    ("on_call", "known", "expected"),
    [(True, False, False), (True, None, None), (False, None, None)],
)
def test_call_status_passes_none_through_only_when_unknown(
    session: Callable[..., PassiveRequest],
    app: Engine,
    on_call: bool,
    known: bool | None,
    expected: bool | None,
) -> None:
    obs = SimSessionTelemetry(app, TIMEOUT_MS).call_status(
        session(on_call=on_call, call_counterparty_known=known)
    )
    assert obs.on_call is on_call
    assert obs.counterparty_in_contacts is expected


def test_missing_telemetry_and_device_raise(app: Engine) -> None:
    adapter = SimSessionTelemetry(app, TIMEOUT_MS)
    request = _request(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), None)
    with pytest.raises(LookupError, match="device"):
        adapter.device_posture(request)
    with pytest.raises(LookupError, match="telemetry"):
        adapter.call_status(request)


# ---------------------------------------------------------------- statement_timeout


def test_queries_run_with_the_statement_timeout(app: Engine) -> None:
    from delphi.probes.sim_adapters import _read

    with _read(app, 1234) as conn:
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() == "1234ms"
        with pytest.raises(Exception, match="statement timeout"):
            conn.execute(text("SELECT pg_sleep(5)"))
    # SET LOCAL: the next transaction on a pooled connection is back to the default
    with app.connect() as conn:
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() != "1234ms"


# ---------------------------------------------------------------- end to end


def test_the_runner_classifies_every_passive_probe_from_the_bank(
    kb: KnowledgeBase, session: Callable[..., PassiveRequest], app: Engine
) -> None:
    request = session(device_age=timedelta(days=1), on_call=True, call_counterparty_known=False)
    telemetry = SimSessionTelemetry(app, TIMEOUT_MS)
    fetchers = passive_fetchers(
        SimBeneficiaryIntel(app, FixedMuleScore(0.9), TIMEOUT_MS),
        SimPhoneRisk(app, TIMEOUT_MS),
        telemetry,
        telemetry,
    )
    pool = passive_executor(threads=4)
    try:
        results = asyncio.run(
            run_passive_probes(
                list(fetchers),
                fetchers,
                request,
                kb.config.classifiers,
                kb.config.guardrails.passive_adapter_timeout_ms * 10,
                executor=pool,
            )
        )
    finally:
        pool.shutdown()
    assert all(r.status is PassiveStatus.OK for r in results), results
    assert {r.source_key: r.outcome for r in results} == {
        "BENEFICIARY_INTEL": "HIGH_MULE_SCORE",
        "COUNTERPARTY_PHONE_RISK": "LOW_OR_UNKNOWN",
        "DEVICE_POSTURE": "NEW_DEVICE",
        "SESSION_BEHAVIOUR": "NORMAL",
        "CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER",
    }
