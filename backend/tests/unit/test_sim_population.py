"""The synthetic bank's population (docs/14 §2–3, FR-SIM-01, #17)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sim.generator_config import GeneratorConfig, history_end, load_generator_config
from delphi.sim.population import Population, generate_population


@pytest.fixture(scope="module")
def config() -> GeneratorConfig:
    return load_generator_config()


@pytest.fixture(scope="module")
def vulnerable_age(kb: KnowledgeBase) -> int:
    return kb.config.engine.vulnerable_age_threshold


@pytest.fixture(scope="module")
def demo(config: GeneratorConfig, vulnerable_age: int) -> Population:
    """demo-sized population, at the fixed epoch, for the distribution checks."""
    return generate_population(
        config,
        "demo",
        history_end=config.fixed_history_end.astimezone(UTC),
        vulnerable_age=vulnerable_age,
    )


def test_profiles_match_the_spec(config: GeneratorConfig) -> None:
    assert {
        name: (p.customers, p.history_days, p.maturity_days, p.window_days, p.seed)
        for name, p in config.profiles.items()
    } == {
        "test": (200, 21, 7, 3, 1),
        "demo": (2000, 150, 60, None, 20261031),
        "eval": (20000, 150, 60, 30, 7),
    }


def test_history_end(config: GeneratorConfig) -> None:
    epoch = datetime(2026, 6, 30, 18, 30, tzinfo=UTC)  # 2026-07-01T00:00:00+05:30
    assert history_end(config, "test") == epoch
    assert history_end(config, "eval") == epoch
    created = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert history_end(config, "demo", created_at=created) == created
    with pytest.raises(ValueError, match="creation time"):
        history_end(config, "demo")
    with pytest.raises(ValueError, match="unknown profile"):
        generate_population(config, "nope", history_end=epoch, vulnerable_age=70)


def test_counts_follow_the_profile(demo: Population) -> None:
    assert len(demo.customers) == 2000
    merchants = [a for a in demo.accounts if a.is_merchant]
    assert len(merchants) == 300  # docs/14 §3 (demo)
    assert sum(a.merchant_category == "HOSPITAL" for a in merchants) == 20
    customer_accounts = [a for a in demo.accounts if a.customer_id is not None]
    assert len(customer_accounts) == 2000
    assert {a.bank_code for a in customer_accounts} == {"DLPH"}
    assert demo.history_end - demo.history_start == timedelta(days=150)


def _share(values: list[bool]) -> float:
    return sum(values) / len(values)


def test_marginals_match_docs_14_section_3(demo: Population, vulnerable_age: int) -> None:
    customers = demo.customers
    ages = [c.age for c in customers]
    assert min(ages) >= 18 and max(ages) <= 85
    assert _share([a >= 70 for a in ages]) == pytest.approx(0.12, abs=0.025)
    assert _share([60 <= a <= 69 for a in ages]) == pytest.approx(0.15, abs=0.025)
    languages = Counter(c.language for c in customers)
    for language, share in {"hi": 0.40, "en": 0.36, "te": 0.12, "ta": 0.12}.items():
        assert languages[language] / len(customers) == pytest.approx(share, abs=0.035)
    incomes = Counter(c.income_band for c in customers)
    for band, share in {"LOW": 0.45, "MID": 0.40, "HIGH": 0.15}.items():
        assert incomes[band] / len(customers) == pytest.approx(share, abs=0.035)
    assert _share([c.consent_call_status for c in customers]) == pytest.approx(0.70, abs=0.035)
    assert _share([c.disability_flag for c in customers]) == pytest.approx(0.02, abs=0.01)
    for c in customers:  # docs/05 §1
        assert c.is_vulnerable == (c.age >= vulnerable_age or c.disability_flag)


def test_devices_and_payees(demo: Population) -> None:
    devices = Counter(d.customer_id for d in demo.devices)
    assert set(devices.values()) <= {1, 2}
    assert _share([n == 2 for n in devices.values()]) == pytest.approx(0.2, abs=0.03)
    # Known devices: first seen well before the history starts, so none looks new.
    assert max(d.first_seen_at for d in demo.devices) < demo.history_start - timedelta(days=7)

    per_customer = Counter(p.customer_id for p in demo.payees)
    assert min(per_customer.values()) >= 3 and max(per_customer.values()) <= 15  # docs/14 §3
    pairs = [(p.customer_id, p.account_id) for p in demo.payees]
    assert len(pairs) == len(set(pairs))  # unique(customer_id, account_id)
    accounts = {a.id: a for a in demo.accounts}
    own = {a.customer_id: a.id for a in demo.accounts if a.customer_id is not None}
    assert all(accounts[p.account_id].id != own[p.customer_id] for p in demo.payees)
    assert not any(accounts[p.account_id].merchant_category == "HOSPITAL" for p in demo.payees)
    home = _share([accounts[p.account_id].bank_code == "DLPH" for p in demo.payees])
    assert home == pytest.approx(0.70, abs=0.03)  # docs/14 §3: 70% of counterparties at DLPH
    assert all(p.added_at < demo.history_start for p in demo.payees)  # established


def test_ids_vpas_and_phones_are_unique(demo: Population) -> None:
    for rows in (demo.customers, demo.accounts, demo.devices, demo.payees):
        ids = [r.id for r in rows]
        assert len(ids) == len(set(ids))
    every_id = [
        r.id for rows in (demo.customers, demo.accounts, demo.devices, demo.payees) for r in rows
    ]
    assert len(every_id) == len(set(every_id))
    vpas = [a.vpa for a in demo.accounts]
    phones = [a.phone_number for a in demo.accounts]
    assert len(set(vpas)) == len(vpas) and len(set(phones)) == len(phones)


def test_balances_and_dates_are_valid(demo: Population) -> None:
    assert all(a.balance_paise >= 0 for a in demo.accounts)  # bank.accounts CHECK
    assert all(a.opened_at < demo.history_start for a in demo.accounts)
    by_band: dict[str, list[int]] = {}
    accounts = {a.customer_id: a for a in demo.accounts if a.customer_id is not None}
    for c in demo.customers:
        by_band.setdefault(c.income_band, []).append(accounts[c.id].balance_paise)
    medians = {band: sorted(v)[len(v) // 2] for band, v in by_band.items()}
    assert medians["LOW"] < medians["MID"] < medians["HIGH"]  # amounts scale with income


def test_config_rejects_a_bad_distribution(config: GeneratorConfig) -> None:
    data = config.model_dump()
    data["population"]["language"]["hi"] = 0.5
    with pytest.raises(ValueError, match="language must be a probability distribution"):
        GeneratorConfig.model_validate(data)
