"""The generator's configuration, `sim/config/generator.yaml` (docs/14 §2–4, #17)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from delphi.sim.response_models import CONFIG_DIR

GENERATOR_CONFIG_FILE = CONFIG_DIR / "generator.yaml"

Probability = Annotated[float, Field(ge=0, le=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _distribution(weights: dict[str, float], where: str) -> dict[str, float]:
    if not weights or min(weights.values()) < 0 or not math.isclose(sum(weights.values()), 1):
        raise ValueError(f"{where} must be a probability distribution")
    return weights


class ProfileConfig(_Frozen):
    customers: int = Field(ge=1)
    history_days: int = Field(ge=1)
    maturity_days: int = Field(ge=1)
    window_days: int | None = Field(default=None, ge=1)
    seed: int = Field(ge=0)


class Range(_Frozen):
    min: float
    max: float


class LogNormal(_Frozen):
    median: float = Field(gt=0)
    sigma: float = Field(gt=0)


class PopulationConfig(_Frozen):
    age_bands: tuple[tuple[int, int, Probability], ...]
    language: dict[str, float]
    disability_probability: Probability
    income_band: dict[str, float]
    income_amount_scale: dict[str, float]
    consent_call_status_probability: Probability
    device_count: dict[int, float]
    payee_count: Range
    counterparty_home_bank_share: Probability
    external_banks: tuple[str, ...]
    segment: str
    device_os: dict[str, float]
    payee_type: dict[str, float]
    account_tenure_years: Range
    opening_balance_inr: LogNormal
    home_city: dict[str, dict[str, float]]
    known_device_min_age_days: int = Field(ge=0)
    young_account_share: Probability
    young_account_days: Range
    genuine_kyc_minimal_share: Probability
    external_counterparty_income_band: str

    @model_validator(mode="after")
    def _distributions(self) -> PopulationConfig:
        _distribution({str(b): p for b, (_, _, p) in enumerate(self.age_bands)}, "age_bands")
        for name in ("language", "income_band", "device_os", "payee_type"):
            _distribution(getattr(self, name), name)
        _distribution({str(k): v for k, v in self.device_count.items()}, "device_count")
        for language, cities in self.home_city.items():
            _distribution(cities, f"home_city.{language}")
        if set(self.home_city) != set(self.language):
            raise ValueError("home_city must cover every language")
        if set(self.income_amount_scale) != set(self.income_band):
            raise ValueError("income_amount_scale must cover every income band")
        if self.young_account_days.min < self.known_device_min_age_days:
            raise ValueError("young accounts must be older than a known device")
        if self.external_counterparty_income_band not in self.income_band:
            raise ValueError("external_counterparty_income_band must be an income band")
        return self


class MerchantConfig(_Frozen):
    per_customer: float = Field(gt=0)
    hospital_one_in: int = Field(ge=1)
    categories: dict[str, float]

    @model_validator(mode="after")
    def _categories(self) -> MerchantConfig:
        _distribution(self.categories, "merchants.categories")
        return self


class NewPersonPayeeConfig(_Frozen):
    """Genuine customers saving new person payees during the history (#56)."""

    mean_days: float = Field(gt=0)
    lead_minutes: Range
    follow_up_payments: Range
    follow_up_days: float = Field(gt=0)


class GenuineRoutineConfig(_Frozen):
    daily_rate_gamma: dict[str, float]
    zipf_exponent: float = Field(ge=0)
    amount_inr: dict[str, LogNormal]
    habitual_hour: Range
    hour_sd: float = Field(gt=0)
    channel: dict[str, dict[str, float]]
    salary_inr: LogNormal
    salary_hour: Range
    payroll_opening_balance_inr: int = Field(gt=0)
    payroll_account_age_days: int = Field(ge=0)
    new_person_payee: NewPersonPayeeConfig

    @model_validator(mode="after")
    def _shapes(self) -> GenuineRoutineConfig:
        if set(self.daily_rate_gamma) != {"shape", "scale"}:
            raise ValueError("daily_rate_gamma needs shape and scale")
        for payee_type, channels in self.channel.items():
            _distribution(channels, f"genuine_routine.channel.{payee_type}")
        if set(self.channel) != set(self.amount_inr):
            raise ValueError("genuine_routine needs a channel mix and an amount per payee type")
        return self


class TelemetryShape(_Frozen):
    paste_share: Probability
    screen_share_share: Probability
    normal_dwell_s: Range
    long_dwell_extra_s: LogNormal
    cadence_excess_z: float = Field(gt=0)
    session_duration_s: LogNormal


class GenuineUnusualConfig(_Frozen):
    rate_per_10k: float = Field(gt=0)
    routine_rate_per_10k: float = Field(gt=0)
    night_share: Probability
    merchant_categories: tuple[str, ...]
    kind: dict[str, float]
    night_hours: Range
    amount_inr: LogNormal
    lead_minutes: Range
    channel: str
    max_balance_share: Probability
    made_share: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _kinds(self) -> GenuineUnusualConfig:
        _distribution(self.kind, "genuine_unusual.kind")
        if set(self.kind) != {"NEW_MERCHANT", "NEW_PHONE"}:
            raise ValueError("genuine_unusual.kind must be NEW_MERCHANT and NEW_PHONE")
        return self


class City(_Frozen):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class LegacyPolicyConfig(_Frozen):
    """Baseline B1 (docs/22 §2)."""

    hold_new_payee_min_inr: int = Field(gt=0)
    hold_new_device_min_inr: int = Field(gt=0)
    hold_collect_approval_min_inr: int = Field(gt=0)
    new_payee_hours: int = Field(gt=0)
    payee_baseline_days: int = Field(gt=0)


class MuleRingConfig(_Frozen):
    """Mule networks (docs/14 §4)."""

    customers_per_ring: int = Field(gt=0)
    min_rings: int = Field(ge=1)
    size: Range
    layers: Range
    home_bank_share: Probability
    shared_device_probability: Probability
    shared_device_min_accounts: int = Field(ge=2)
    age: Range
    minimal_kyc_share: Probability
    opened_days_before_history: Range
    opening_balance_inr: LogNormal
    false_listing_share: Probability

    @model_validator(mode="after")
    def _ring_fits(self) -> MuleRingConfig:
        if not 1 <= self.layers.min <= self.layers.max <= self.size.min <= self.size.max:
            raise ValueError("mule_rings needs 1 ≤ layers ≤ size, so every layer has an account")
        if self.shared_device_min_accounts >= self.size.min:
            raise ValueError("mule_rings.shared_device_min_accounts exceeds the smallest ring")
        return self


class AtoConfig(_Frozen):
    transfers: Range
    drain_share: Range
    window_minutes: float = Field(gt=0)
    kind: dict[str, float]
    sim_swap_lead_hours: Range
    payee_lead_minutes: Range
    channel: dict[str, float]

    @model_validator(mode="after")
    def _weights(self) -> AtoConfig:
        _distribution(self.kind, "fraud.ato.kind")
        _distribution(self.channel, "fraud.ato.channel")
        if self.payee_lead_minutes.max >= self.window_minutes:
            raise ValueError("fraud.ato: the payee must be added inside the transfer window")
        return self


class DigitalArrestConfig(_Frozen):
    older_share: Probability
    older_age: int = Field(ge=18)
    transfers: Range
    span_days: Range
    balance_share: Range
    channel: dict[str, float]

    @model_validator(mode="after")
    def _weights(self) -> DigitalArrestConfig:
        _distribution(self.channel, "fraud.digital_arrest.channel")
        return self


class InvestmentConfig(_Frozen):
    payments: Range
    fee_payments: Range
    span_days: Range
    fee_phase_share: Probability
    first_amount_inr: LogNormal
    escalation: float = Field(ge=1)
    fee_share: Probability
    max_balance_share: Probability
    channel: dict[str, float]

    @model_validator(mode="after")
    def _weights(self) -> InvestmentConfig:
        _distribution(self.channel, "fraud.investment.channel")
        return self


class FraudConfig(_Frozen):
    """The fraud victims' scenarios (docs/14 §4)."""

    rate_per_10k: dict[str, float]
    genuine_rate_per_10k: float = Field(gt=0)
    payee_lead_minutes: Range
    ato: AtoConfig
    digital_arrest: DigitalArrestConfig
    investment: InvestmentConfig
    complaint_channel: dict[str, float]
    complaint_category: dict[str, str]

    @model_validator(mode="after")
    def _scenarios(self) -> FraudConfig:
        _distribution(self.complaint_channel, "fraud.complaint_channel")
        scenarios = {"ATO", "APP_DIGITAL_ARREST", "APP_INVESTMENT"}
        if set(self.rate_per_10k) != scenarios or set(self.complaint_category) != scenarios:
            raise ValueError("fraud needs a rate and a complaint category per victim scenario")
        return self


class CollectRefundConfig(_Frozen):
    """The hidden UPI_COLLECT_REFUND scenario, live/eval window only (docs/14 §4)."""

    rate_per_10k: float = Field(gt=0)
    amount_inr: Range
    channel: str
    max_balance_share: Probability


class CrmConfig(_Frozen):
    """Genuine customers' background CRM events."""

    background_mean_days: float = Field(gt=0)
    types: dict[str, float]

    @model_validator(mode="after")
    def _weights(self) -> CrmConfig:
        _distribution(self.types, "crm.types")
        return self


class ForwardingConfig(_Frozen):
    """Money moving down a mule ring (docs/14 §4)."""

    first_layer_dwell_minutes: LogNormal
    deeper_dwell_minutes: LogNormal
    splits: Range
    cash_out_share: Probability
    registry_listing_share: Probability
    split_gap_minutes: Range
    channel: dict[str, float]
    cash_account_age_days: int = Field(ge=0)
    rate_per_10k: float = Field(gt=0)
    payee_lead_minutes: Range

    @model_validator(mode="after")
    def _weights(self) -> ForwardingConfig:
        _distribution(self.channel, "forwarding.channel")
        if self.splits.min < 1:
            raise ValueError("forwarding.splits must be at least 1")
        return self


class FriConfig(_Frozen):
    ring_numbers: dict[str, float]
    genuine_numbers: dict[str, float]

    @model_validator(mode="after")
    def _weights(self) -> FriConfig:
        for name in ("ring_numbers", "genuine_numbers"):
            weights = getattr(self, name)
            _distribution(weights, f"external_systems.fri.{name}")
            if set(weights) != {"VERY_HIGH", "HIGH", "MEDIUM", "LOW"}:
                raise ValueError("FRI categories are VERY_HIGH, HIGH, MEDIUM and LOW")
        return self


class CfcfrmsConfig(_Frozen):
    ack_minutes: LogNormal
    never_ack_share: Probability
    ack_deadline_minutes: float = Field(gt=0)


class ExternalSystemsConfig(_Frozen):
    """The simulated external systems (docs/14 §9)."""

    fri: FriConfig
    cfcfrms: CfcfrmsConfig


class GeneratorConfig(_Frozen):
    profiles: dict[str, ProfileConfig]
    fixed_history_end: datetime
    population: PopulationConfig
    merchants: MerchantConfig
    genuine_routine: GenuineRoutineConfig
    telemetry: TelemetryShape
    legacy_policy: LegacyPolicyConfig
    genuine_unusual: GenuineUnusualConfig
    mule_rings: MuleRingConfig
    fraud: FraudConfig
    collect_refund: CollectRefundConfig
    forwarding: ForwardingConfig
    crm: CrmConfig
    external_systems: ExternalSystemsConfig
    new_device_mean_days: float = Field(gt=0)
    login_lead_s: Range
    cities: dict[str, City]

    @model_validator(mode="after")
    def _home_cities_have_coordinates(self) -> GeneratorConfig:
        named = {city for cities in self.population.home_city.values() for city in cities}
        missing = sorted(named - set(self.cities))
        if missing:
            raise ValueError(f"cities has no coordinates for {', '.join(missing)}")
        return self


def load_generator_config(path: Path = GENERATOR_CONFIG_FILE) -> GeneratorConfig:
    return GeneratorConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def history_end(
    config: GeneratorConfig, profile: str, *, created_at: datetime | None = None
) -> datetime:
    """docs/14 §2: demo ends its history when the dataset is created; eval and test at a fixed
    virtual epoch, so their datasets are byte-identical on every run."""
    if profile == "demo":
        if created_at is None:
            raise ValueError("the demo profile's history ends at its creation time")
        return created_at.astimezone(UTC)
    return config.fixed_history_end.astimezone(UTC)
