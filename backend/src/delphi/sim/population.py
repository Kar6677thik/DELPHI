"""The synthetic bank's population: customers, accounts, devices, payees (docs/14 §2–3, #17).

Pure and deterministic: the same profile, seed and config give the same population, byte for
byte (FR-SIM-01). Each customer draws from its own stream, ``keyed_rng(seed, "customer", i)``,
and the merchants from ``keyed_rng(seed, "merchants")``, so one component's draws never shift
another's. Ids come from per-kind ``IdStream``s (docs/14 §2). Every number is in
``config/generator.yaml``.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import numpy as np

from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.ids import IdStream
from delphi.sim.rng import keyed_rng

HOME_BANK = "DLPH"
HOSPITAL = "HOSPITAL"
ACTIVE = "ACTIVE"
FULL_KYC = "FULL"
_PAISE_PER_INR = 100
_DAYS_PER_YEAR = 365
_SECONDS_PER_DAY = 86_400
# Known devices were first seen at least this long before the history starts.
_KNOWN_DEVICE_MIN_AGE_DAYS = 30
_FINGERPRINT_BYTES = 16


# --------------------------------------------------------------------------- the entities
# Fields mirror the bank tables (delphi.sim.bank) minus `created_at`, which the database sets.


@dataclass(frozen=True, slots=True)
class Customer:
    id: UUID
    display_name: str
    age: int
    language: str
    is_vulnerable: bool
    disability_flag: bool
    segment: str
    income_band: str
    consent_call_status: bool
    home_city: str
    persona_key: str | None = None


@dataclass(frozen=True, slots=True)
class Account:
    id: UUID
    customer_id: UUID | None
    bank_code: str
    is_merchant: bool
    merchant_category: str | None
    opened_at: datetime
    kyc_level: str
    status: str
    balance_paise: int
    phone_number: str | None
    vpa: str | None


@dataclass(frozen=True, slots=True)
class Device:
    id: UUID
    customer_id: UUID
    fingerprint: str
    os: str
    first_seen_at: datetime


@dataclass(frozen=True, slots=True)
class Payee:
    id: UUID
    customer_id: UUID
    account_id: UUID
    label: str
    added_at: datetime


@dataclass(frozen=True, slots=True)
class Population:
    profile: str
    history_start: datetime
    history_end: datetime
    customers: tuple[Customer, ...]
    accounts: tuple[Account, ...]
    devices: tuple[Device, ...]
    payees: tuple[Payee, ...]


# --------------------------------------------------------------------------- generation


_CDFS: dict[tuple[tuple[str, float], ...], tuple[tuple[str, ...], list[float]]] = {}


def _choice(rng: np.random.Generator, weights: dict[str, float]) -> str:
    """One draw from a categorical distribution (one uniform, inverse CDF)."""
    key = tuple(weights.items())
    if key not in _CDFS:
        totals = np.cumsum([w for _, w in key])
        _CDFS[key] = (tuple(k for k, _ in key), [float(t) for t in totals / totals[-1]])
    keys, cdf = _CDFS[key]
    return keys[min(bisect.bisect_right(cdf, rng.random()), len(keys) - 1)]


def _between(rng: np.random.Generator, start: datetime, end: datetime) -> datetime:
    """A uniformly random instant in [start, end), to the second."""
    seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=int(rng.integers(0, max(seconds, 1))))


class _Builder:
    def __init__(
        self, config: GeneratorConfig, profile: str, end: datetime, vulnerable_age: int
    ) -> None:
        self.cfg = config.population
        self.merchant_cfg = config.merchants
        self.spec = config.profiles[profile]
        self.profile = profile
        self.end = end
        self.start = end - timedelta(days=self.spec.history_days)
        self.vulnerable_age = vulnerable_age
        seed = self.spec.seed
        self.ids = {
            kind: IdStream(seed, kind)
            for kind in ("customer", "merchant_account", "external_account", "device", "payee")
        }
        self.customer_account_ids = IdStream(seed, "customer_account").take(self.spec.customers)
        self.accounts: list[Account] = []
        self.merchants_by_category: dict[str, list[UUID]] = {}
        self.external_count = 0

    def _bank(self, rng: np.random.Generator) -> str:
        if rng.random() < self.cfg.counterparty_home_bank_share:
            return HOME_BANK
        return self.cfg.external_banks[int(rng.integers(len(self.cfg.external_banks)))]

    def _opened_at(self, rng: np.random.Generator) -> datetime:
        tenure = self.cfg.account_tenure_years
        years = tenure.min + (tenure.max - tenure.min) * rng.random()
        return self.start - timedelta(seconds=int(years * _DAYS_PER_YEAR * _SECONDS_PER_DAY))

    def _balance(self, rng: np.random.Generator, income_band: str) -> int:
        opening = self.cfg.opening_balance_inr
        median = opening.median * self.cfg.income_amount_scale[income_band] * _PAISE_PER_INR
        return round(median * math.exp(opening.sigma * rng.standard_normal()))

    def merchants(self) -> None:
        rng = keyed_rng(self.spec.seed, "merchants")
        count = round(self.spec.customers * self.merchant_cfg.per_customer)
        hospitals = round(count * self.merchant_cfg.hospital_share)
        for i in range(count):
            category = HOSPITAL if i < hospitals else _choice(rng, self.merchant_cfg.categories)
            bank_code = self._bank(rng)
            account = Account(
                id=self.ids["merchant_account"].next(),
                customer_id=None,
                bank_code=bank_code,
                is_merchant=True,
                merchant_category=category,
                opened_at=self._opened_at(rng),
                kyc_level=FULL_KYC,
                status=ACTIVE,
                balance_paise=0,
                phone_number=f"+918{i:09d}",
                vpa=f"merchant{i:05d}@{bank_code.lower()}",
            )
            self.accounts.append(account)
            self.merchants_by_category.setdefault(category, []).append(account.id)

    def _merchant_payee(self, rng: np.random.Generator, payee_type: str) -> tuple[UUID, str]:
        # Hospitals are never established payees: paying one is a GENUINE_UNUSUAL event.
        if payee_type == "UTILITY" and self.merchants_by_category.get("UTILITY"):
            category = "UTILITY"
        else:
            options = [c for c in self.merchants_by_category if c not in (HOSPITAL, "UTILITY")]
            category = options[int(rng.integers(len(options)))]
        accounts = self.merchants_by_category[category]
        return accounts[int(rng.integers(len(accounts)))], category.title().replace("_", " ")

    def _family_payee(self, rng: np.random.Generator, own: int, k: int) -> tuple[UUID, str]:
        label = f"Family {k + 1}"
        if rng.random() < self.cfg.counterparty_home_bank_share and self.spec.customers > 1:
            other = int(rng.integers(self.spec.customers - 1))
            other += other >= own  # anyone but the customer themself
            return self.customer_account_ids[other], label
        bank_code = self.cfg.external_banks[int(rng.integers(len(self.cfg.external_banks)))]
        index = self.external_count
        self.external_count += 1
        account = Account(
            id=self.ids["external_account"].next(),
            customer_id=None,
            bank_code=bank_code,
            is_merchant=False,
            merchant_category=None,
            opened_at=self._opened_at(rng),
            kyc_level=FULL_KYC,
            status=ACTIVE,
            balance_paise=self._balance(rng, "MID"),
            phone_number=f"+917{index:09d}",
            vpa=f"person{index:06d}@{bank_code.lower()}",
        )
        self.accounts.append(account)
        return account.id, label

    def customer(self, i: int, devices: list[Device], payees: list[Payee]) -> Customer:
        cfg = self.cfg
        rng = keyed_rng(self.spec.seed, "customer", str(i))
        band = int(_choice(rng, {str(b): p for b, (_, _, p) in enumerate(cfg.age_bands)}))
        low, high, _ = cfg.age_bands[band]
        age = int(rng.integers(low, high + 1))
        language = _choice(rng, cfg.language)
        disability = bool(rng.random() < cfg.disability_probability)
        income_band = _choice(rng, cfg.income_band)
        customer = Customer(
            id=self.ids["customer"].next(),
            display_name=f"Customer {i:05d}",
            age=age,
            language=language,
            is_vulnerable=age >= self.vulnerable_age or disability,
            disability_flag=disability,
            segment=cfg.segment,
            income_band=income_band,
            consent_call_status=bool(rng.random() < cfg.consent_call_status_probability),
            home_city=_choice(rng, cfg.home_city[language]),
        )
        opened_at = self._opened_at(rng)
        self.accounts.append(
            Account(
                id=self.customer_account_ids[i],
                customer_id=customer.id,
                bank_code=HOME_BANK,
                is_merchant=False,
                merchant_category=None,
                opened_at=opened_at,
                kyc_level=FULL_KYC,
                status=ACTIVE,
                balance_paise=self._balance(rng, income_band),
                phone_number=f"+919{i:09d}",
                vpa=f"customer{i:06d}@dlph",
            )
        )
        known_until = self.start - timedelta(days=_KNOWN_DEVICE_MIN_AGE_DAYS)
        device_counts = {str(k): v for k, v in cfg.device_count.items()}
        for _ in range(int(_choice(rng, device_counts))):
            devices.append(
                Device(
                    id=self.ids["device"].next(),
                    customer_id=customer.id,
                    fingerprint=rng.bytes(_FINGERPRINT_BYTES).hex(),
                    os=_choice(rng, cfg.device_os),
                    first_seen_at=_between(rng, opened_at, known_until),
                )
            )
        chosen: set[UUID] = set()
        count = int(rng.integers(int(cfg.payee_count.min), int(cfg.payee_count.max) + 1))
        for k in range(count):
            payee_type = _choice(rng, cfg.payee_type)
            if payee_type == "FAMILY":
                account_id, label = self._family_payee(rng, i, k)
            else:
                account_id, label = self._merchant_payee(rng, payee_type)
            if account_id in chosen:
                continue  # a customer saves each beneficiary once (unique per customer)
            chosen.add(account_id)
            payees.append(
                Payee(
                    id=self.ids["payee"].next(),
                    customer_id=customer.id,
                    account_id=account_id,
                    label=label,
                    added_at=_between(rng, opened_at, self.start),
                )
            )
        return customer


def generate_population(
    config: GeneratorConfig, profile: str, *, history_end: datetime, vulnerable_age: int
) -> Population:
    """The profile's customers with their accounts, devices and established payees, plus the
    merchants and external counterparties they pay. ``vulnerable_age`` is the KB's
    ``engine.vulnerable_age_threshold`` (docs/05 §1: vulnerable = age ≥ 70 or disability)."""
    if profile not in config.profiles:
        raise ValueError(f"unknown profile {profile!r}")
    builder = _Builder(config, profile, history_end, vulnerable_age)
    builder.merchants()
    customers: list[Customer] = []
    devices: list[Device] = []
    payees: list[Payee] = []
    for i in range(builder.spec.customers):
        customers.append(builder.customer(i, devices, payees))
    return Population(
        profile=profile,
        history_start=builder.start,
        history_end=history_end,
        customers=tuple(customers),
        accounts=tuple(builder.accounts),
        devices=tuple(devices),
        payees=tuple(payees),
    )
