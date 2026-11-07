"""The demo personas' configuration, `sim/config/personas.yaml` (docs/14 §6, #19).

Only data: the generator (`delphi.sim.population`, `.mules`, `.personas`) builds the rows, and
the story runners (M4–M7) read the story scripts.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from delphi.sim.response_models import CONFIG_DIR

PERSONA_CONFIG_FILE = CONFIG_DIR / "personas.yaml"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PersonaCustomer(_Frozen):
    """A scripted customer: the normal customer draws, then these values."""

    key: str
    display_name: str
    age: int = Field(ge=18)
    language: str
    home_city: str
    income_band: str
    consent_call_status: bool
    devices: int = Field(ge=1)
    tenure_years: float = Field(gt=0)
    opening_balance_inr: int = Field(gt=0)


class S4Victims(_Frozen):
    count: int = Field(ge=0)
    key_prefix: str


class PersonaRing(_Frozen):
    """A scripted mule ring: the banks of its accounts, layer by layer."""

    key: str
    layers: tuple[tuple[str, ...], ...]
    shared_device: bool
    opened_days_before_end: float | None = None  # before the history ends; else drawn
    kyc_level: str | None = None  # else drawn as for any mule
    fri: str | None = None  # the FRI category of its numbers, else drawn

    @model_validator(mode="after")
    def _layers(self) -> PersonaRing:
        if not self.layers or not all(self.layers):
            raise ValueError(f"persona ring {self.key}: every layer needs an account")
        return self


class PersonaMerchant(_Frozen):
    """A generated merchant of this category that stands in for a named one."""

    key: str
    name: str
    category: str
    fri: str


class StoryTelemetry(_Frozen):
    typing_cadence_z: float
    paste_on_account_field: bool
    max_field_dwell_s: float
    remote_access_active: bool
    screen_share_active: bool
    on_call: bool
    call_counterparty_known: bool | None
    session_duration_s: float


class StoryAnswer(_Frozen):
    probe: str
    answer: str
    after_s: float = Field(ge=0)
    app_switches: int = Field(default=0, ge=0)


class Story(_Frozen):
    """A demo story's scripted payment (docs/14 §6), for the story runners."""

    customer: str
    payee: str  # a persona merchant, or a persona ring (its first layer-1 account)
    amount_inr: int = Field(gt=0)
    channel: str
    local_time: str | None = None  # IST "HH:MM"; else at the story's T
    minutes_before_t0: float | None = None  # made this long before T (S-3)
    new_device_os: str | None = None  # a phone first seen at the story's T (S-2)
    telemetry: StoryTelemetry | None = None
    answers: tuple[StoryAnswer, ...] = ()
    complaint_channel: str | None = None  # a complaint at T (S-3)


class PersonaConfig(_Frozen):
    profiles: tuple[str, ...]
    customers: tuple[PersonaCustomer, ...]
    s4_victims: S4Victims
    rings: tuple[PersonaRing, ...]
    merchants: tuple[PersonaMerchant, ...]
    stories: dict[str, Story]

    @model_validator(mode="after")
    def _references(self) -> PersonaConfig:
        customers = {c.key for c in self.customers}
        payees = {r.key for r in self.rings} | {m.key for m in self.merchants}
        for name, story in self.stories.items():
            if story.customer not in customers or story.payee not in payees:
                raise ValueError(f"story {name} names an unknown persona")
        keys = [*customers, *payees]
        if len(keys) != len(set(keys)):
            raise ValueError("persona keys must be unique")
        return self

    @property
    def keys(self) -> tuple[str, ...]:
        """Every persona customer's key, the S-4 victims included, in generation order."""
        victims = self.s4_victims
        return (
            *(c.key for c in self.customers),
            *(f"{victims.key_prefix}_{n:02d}" for n in range(1, victims.count + 1)),
        )


def load_persona_config(path: Path = PERSONA_CONFIG_FILE) -> PersonaConfig:
    return PersonaConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
