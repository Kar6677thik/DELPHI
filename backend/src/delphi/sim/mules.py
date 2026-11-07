"""Mule networks and the sim suspect registry's false listings (docs/14 §4, §7, §9; #17).

Rings of 5–30 accounts across banks, in 1–4 layers. A home-bank (DLPH) mule is a customer of
the bank, with a device; an external-bank mule is an account only, like every external
counterparty. With probability 0.5 a ring shares one device fingerprint across ≥ 3 of its
home-bank mules. The flows through the rings, and the 40% of mule accounts the registry lists
after their first fraud credit, come with the fraud scenarios.

False listings: 0.1% of genuine non-merchant accounts are listed, each on a uniform instant in
the history, and get a (wrong) MULE label then. Each ring draws from
``keyed_rng(seed, "mule_ring", r)``, each candidate account from
``keyed_rng(seed, "false_listing", account_id)``. Mule customers, phones and VPAs follow the
population's naming, so nothing in the bank tables gives a mule away.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import numpy as np

from delphi.sim.draws import between, choice
from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.ids import IdStream
from delphi.sim.labels import MULE, LabelSource, ScheduledLabel, SubjectType
from delphi.sim.persona_config import PersonaConfig, PersonaRing
from delphi.sim.population import (
    ACTIVE,
    FULL_KYC,
    HOME_BANK,
    MINIMAL_KYC,
    Account,
    Customer,
    Device,
    Population,
)
from delphi.sim.rng import keyed_rng

MULE_INCOME_BAND = "LOW"
_PAISE_PER_INR = 100
_FINGERPRINT_BYTES = 16


@dataclass(frozen=True, slots=True)
class MuleRing:
    id: UUID
    layers: tuple[tuple[UUID, ...], ...]  # account ids per layer, layer 1 first
    shared_fingerprint: str | None  # the device shared by ≥ 3 home-bank mules, if any
    persona_key: str | None = None  # a scripted demo ring (docs/14 §6), kept out of the history


@dataclass(frozen=True, slots=True)
class AccountTruth:
    """A row of ``sim_truth.account_truth`` (docs/17)."""

    account_id: UUID
    is_mule: bool
    ring_id: UUID | None
    layer: int | None


@dataclass(frozen=True, slots=True)
class RegistryListing:
    """A row of ``bank.sim_registry_listings`` (docs/14 §9)."""

    account_id: UUID
    listed_at: datetime


@dataclass(frozen=True, slots=True)
class MuleNetwork:
    rings: tuple[MuleRing, ...]
    customers: tuple[Customer, ...]  # the home-bank mules' customers
    accounts: tuple[Account, ...]
    devices: tuple[Device, ...]
    mule_truth: tuple[AccountTruth, ...]  # one row per mule account
    listings: tuple[RegistryListing, ...]
    labels: tuple[ScheduledLabel, ...]  # REGISTRY_LISTING labels for the listings


def ring_count(config: GeneratorConfig, customers: int) -> int:
    cfg = config.mule_rings
    return max(cfg.min_rings, round(customers / cfg.customers_per_ring))


def _layer_sizes(size: int, layers: int) -> list[int]:
    """As even as possible, the extra accounts in the first layers."""
    base, extra = divmod(size, layers)
    return [base + (1 if layer < extra else 0) for layer in range(layers)]


class _RingBuilder:
    def __init__(self, config: GeneratorConfig, population: Population) -> None:
        self.cfg = config.mule_rings
        self.pop_cfg = config.population
        self.pop = population
        self.seed = config.profiles[population.profile].seed
        self.ids = {
            kind: IdStream(self.seed, kind)
            for kind in ("mule_ring", "mule_customer", "mule_account", "mule_device")
        }
        # Mule customers and external mule accounts continue the population's numbering.
        self.next_customer = len(population.customers)
        self.next_external = sum(
            a.customer_id is None and not a.is_merchant for a in population.accounts
        )
        self.customers: list[Customer] = []
        self.accounts: list[Account] = []
        self.devices: list[Device] = []
        self.truth: list[AccountTruth] = []

    def _banks(self, rng: np.random.Generator, size: int, shared: bool) -> list[str]:
        external = self.pop_cfg.external_banks
        banks = [
            HOME_BANK
            if rng.random() < self.cfg.home_bank_share
            else external[int(rng.integers(len(external)))]
            for _ in range(size)
        ]
        if shared:  # a shared device needs enough home-bank mules: turn random ones home
            for j in rng.permutation(size):
                if banks.count(HOME_BANK) >= self.cfg.shared_device_min_accounts:
                    break
                banks[int(j)] = HOME_BANK
        if len(set(banks)) == 1:  # docs/14: rings span banks
            others = [b for b in (HOME_BANK, *external) if b != banks[0]]
            banks[-1] = others[int(rng.integers(len(others)))]
        return banks

    def _account(
        self,
        rng: np.random.Generator,
        bank: str,
        customer_id: UUID | None,
        index: int,
        script: PersonaRing | None = None,
    ) -> Account:
        days = self.cfg.opened_days_before_history
        opened_days = days.min + (days.max - days.min) * rng.random()
        balance = self.cfg.opening_balance_inr
        # In paise, like a genuine customer's: whole-rupee balances would mark mules out.
        paise = (
            balance.median * _PAISE_PER_INR * float(np.exp(balance.sigma * rng.standard_normal()))
        )
        home = customer_id is not None
        opened_at = self.pop.history_start - timedelta(seconds=int(opened_days * 86_400))
        kyc_level = MINIMAL_KYC if rng.random() < self.cfg.minimal_kyc_share else FULL_KYC
        if script is not None and script.opened_days_before_end is not None:
            opened_at = self.pop.history_end - timedelta(days=script.opened_days_before_end)
        if script is not None and script.kyc_level is not None:
            kyc_level = script.kyc_level
        return Account(
            id=self.ids["mule_account"].next(),
            customer_id=customer_id,
            bank_code=bank,
            is_merchant=False,
            merchant_category=None,
            opened_at=opened_at,
            kyc_level=kyc_level,
            status=ACTIVE,
            balance_paise=max(1, round(paise)),
            phone_number=f"+919{index:09d}" if home else f"+917{index:09d}",
            vpa=f"customer{index:06d}@dlph" if home else f"person{index:06d}@{bank.lower()}",
        )

    def _customer(self, rng: np.random.Generator, index: int) -> Customer:
        language = choice(rng, self.pop_cfg.language)
        return Customer(
            id=self.ids["mule_customer"].next(),
            display_name=f"Customer {index:05d}",
            age=int(rng.integers(int(self.cfg.age.min), int(self.cfg.age.max) + 1)),
            language=language,
            is_vulnerable=False,  # young, no disability
            disability_flag=False,
            segment=self.pop_cfg.segment,
            income_band=MULE_INCOME_BAND,
            consent_call_status=bool(rng.random() < self.pop_cfg.consent_call_status_probability),
            home_city=choice(rng, self.pop_cfg.home_city[language]),
        )

    def ring(self, r: int) -> MuleRing:
        cfg = self.cfg
        rng = keyed_rng(self.seed, "mule_ring", str(r))
        ring_id = self.ids["mule_ring"].next()
        size = int(rng.integers(int(cfg.size.min), int(cfg.size.max) + 1))
        n_layers = int(rng.integers(int(cfg.layers.min), int(cfg.layers.max) + 1))
        shared = bool(rng.random() < cfg.shared_device_probability)
        banks = self._banks(rng, size, shared)
        sharers = 0
        fingerprint = None
        if shared:
            home = banks.count(HOME_BANK)
            sharers = int(rng.integers(cfg.shared_device_min_accounts, home + 1))
            fingerprint = rng.bytes(_FINGERPRINT_BYTES).hex()
        by_layer = []
        position = 0
        for layer_size in _layer_sizes(size, n_layers):
            by_layer.append(banks[position : position + layer_size])
            position += layer_size
        layers = self._build(rng, ring_id, by_layer, shared=(sharers, fingerprint))
        return MuleRing(ring_id, layers, fingerprint)

    def persona_ring(self, script: PersonaRing) -> MuleRing:
        """A scripted demo ring (docs/14 §6): its banks layer by layer are fixed; its accounts
        are drawn like any mule's, from ``keyed_rng(seed, "persona_ring", key)``."""
        rng = keyed_rng(self.seed, "persona_ring", script.key)
        ring_id = self.ids["mule_ring"].next()
        banks = [list(layer) for layer in script.layers]
        home = sum(layer.count(HOME_BANK) for layer in banks)
        fingerprint = rng.bytes(_FINGERPRINT_BYTES).hex() if script.shared_device else None
        sharing = (home if fingerprint else 0, fingerprint)
        layers = self._build(rng, ring_id, banks, shared=sharing, script=script)
        return MuleRing(ring_id, layers, fingerprint, persona_key=script.key)

    def _build(
        self,
        rng: np.random.Generator,
        ring_id: UUID,
        banks_by_layer: list[list[str]],
        *,
        shared: tuple[int, str | None],  # how many home-bank mules share which fingerprint
        script: PersonaRing | None = None,
    ) -> tuple[tuple[UUID, ...], ...]:
        sharers, fingerprint = shared
        layers: list[tuple[UUID, ...]] = []
        for layer, banks in enumerate(banks_by_layer, start=1):
            ids = []
            for bank in banks:
                if bank == HOME_BANK:
                    customer = self._customer(rng, self.next_customer)
                    account = self._account(rng, bank, customer.id, self.next_customer, script)
                    self.next_customer += 1
                    self.customers.append(customer)
                    shares = sharers > 0
                    sharers -= shares
                    self.devices.append(
                        Device(
                            id=self.ids["mule_device"].next(),
                            customer_id=customer.id,
                            fingerprint=(
                                fingerprint
                                if shares and fingerprint
                                else rng.bytes(_FINGERPRINT_BYTES).hex()
                            ),
                            os=choice(rng, self.pop_cfg.device_os),
                            first_seen_at=between(rng, account.opened_at, self.pop.history_start),
                        )
                    )
                else:
                    account = self._account(rng, bank, None, self.next_external, script)
                    self.next_external += 1
                self.accounts.append(account)
                self.truth.append(AccountTruth(account.id, True, ring_id, layer))
                ids.append(account.id)
            layers.append(tuple(ids))
        return tuple(layers)


def _false_listings(
    config: GeneratorConfig, population: Population
) -> tuple[list[RegistryListing], list[ScheduledLabel]]:
    seed = config.profiles[population.profile].seed
    span = int((population.history_end - population.history_start).total_seconds())
    listings: list[RegistryListing] = []
    labels: list[ScheduledLabel] = []
    for account in population.accounts:
        if account.is_merchant:
            continue
        rng = keyed_rng(seed, "false_listing", str(account.id))
        listed, offset = rng.random(), rng.random()
        if listed >= config.mule_rings.false_listing_share:
            continue
        at = population.history_start + timedelta(seconds=int(offset * span))
        listings.append(RegistryListing(account.id, at))
        labels.append(
            ScheduledLabel(
                SubjectType.ACCOUNT, str(account.id), MULE, LabelSource.REGISTRY_LISTING, at
            )
        )
    return listings, labels


def generate_mule_network(
    config: GeneratorConfig, population: Population, personas: PersonaConfig | None = None
) -> MuleNetwork:
    """The profile's mule rings, then the personas' scripted rings (docs/14 §6), and the
    registry's false listings of genuine accounts."""
    builder = _RingBuilder(config, population)
    generated = sum(c.persona_key is None for c in population.customers)
    rings = tuple(builder.ring(r) for r in range(ring_count(config, generated)))
    if personas is not None:
        rings += tuple(builder.persona_ring(script) for script in personas.rings)
    listings, labels = _false_listings(config, population)
    return MuleNetwork(
        rings=rings,
        customers=tuple(builder.customers),
        accounts=tuple(builder.accounts),
        devices=tuple(builder.devices),
        mule_truth=tuple(builder.truth),
        listings=tuple(listings),
        labels=tuple(labels),
    )


def account_truth(network: MuleNetwork, accounts: Iterable[Account]) -> tuple[AccountTruth, ...]:
    """A ``sim_truth.account_truth`` row for every account: the mules', and is_mule = false for
    all the others (genuine customers, merchants, external counterparties, payroll)."""
    mules = {t.account_id: t for t in network.mule_truth}
    return tuple(mules.get(a.id, AccountTruth(a.id, False, None, None)) for a in accounts)
