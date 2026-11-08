"""The demo personas (docs/14 §6, #19).

The persona customers (Kamala, Arjun, Ravi and the 15 S-4 victims) are generated after the
ordinary customers (`delphi.sim.population`), and the scripted rings (Kamala's payee, Ring-7)
after the ordinary rings (`delphi.sim.mules`). So adding them changes no other row. No fraud
reaches a persona in the history (`delphi.sim.fraud`): their stories happen live, made by the
`DEMO_STORY` runners from the scripts in `sim/config/personas.yaml`.

The manifest says which rows are whose, for the runners and `GET /phone/personas`
(docs/16). Its ids are deterministic, like every generated id. The `GENERATE_DATASET` job keeps
it in `sim_truth.persona_manifests` (`manifest_json`, `load_manifest`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from delphi.sim.external import PhoneRisk
from delphi.sim.mules import MuleNetwork
from delphi.sim.persona_config import PersonaConfig, Story
from delphi.sim.population import Population


@dataclass(frozen=True, slots=True)
class PersonaManifest:
    customers: Mapping[str, UUID]  # persona key → customer id
    accounts: Mapping[str, UUID]  # persona key → home-bank account id
    devices: Mapping[str, tuple[UUID, ...]]  # persona key → its devices
    merchants: Mapping[str, UUID]  # persona merchant key → the merchant account standing in
    merchant_names: Mapping[str, str]  # persona merchant key → its name
    rings: Mapping[str, tuple[tuple[UUID, ...], ...]]  # persona ring key → accounts by layer
    story_payees: Mapping[str, UUID]  # story → its payee (a merchant, or the ring's first mule)
    stories: Mapping[str, Story]


def _ids(ids: Mapping[str, UUID]) -> dict[str, str]:
    return {k: str(v) for k, v in sorted(ids.items())}


def manifest_json(manifest: PersonaManifest) -> dict[str, Any]:
    """The manifest as JSON, for `sim_truth.persona_manifests`. The story scripts are left out:
    they are `personas.yaml`'s, and ``load_manifest`` takes them from there."""
    return {
        "customers": _ids(manifest.customers),
        "accounts": _ids(manifest.accounts),
        "devices": {k: [str(d) for d in v] for k, v in sorted(manifest.devices.items())},
        "merchants": _ids(manifest.merchants),
        "merchant_names": dict(sorted(manifest.merchant_names.items())),
        "rings": {
            k: [[str(a) for a in layer] for layer in layers]
            for k, layers in sorted(manifest.rings.items())
        },
        "story_payees": _ids(manifest.story_payees),
    }


def load_manifest(data: Mapping[str, Any], config: PersonaConfig) -> PersonaManifest:
    """The manifest ``manifest_json`` stored, with the story scripts of ``config``."""
    return PersonaManifest(
        customers={k: UUID(v) for k, v in data["customers"].items()},
        accounts={k: UUID(v) for k, v in data["accounts"].items()},
        devices={k: tuple(UUID(d) for d in v) for k, v in data["devices"].items()},
        merchants={k: UUID(v) for k, v in data["merchants"].items()},
        merchant_names=dict(data["merchant_names"]),
        rings={
            k: tuple(tuple(UUID(a) for a in layer) for layer in layers)
            for k, layers in data["rings"].items()
        },
        story_payees={k: UUID(v) for k, v in data["story_payees"].items()},
        stories=dict(config.stories),
    )


def persona_manifest(
    config: PersonaConfig, population: Population, network: MuleNetwork
) -> PersonaManifest:
    customers = {c.persona_key: c.id for c in population.customers if c.persona_key}
    by_customer = {c: k for k, c in customers.items()}
    accounts = {
        by_customer[a.customer_id]: a.id
        for a in population.accounts
        if a.customer_id in by_customer
    }
    devices: dict[str, list[UUID]] = {k: [] for k in customers}
    for d in population.devices:
        if d.customer_id in by_customer:
            devices[by_customer[d.customer_id]].append(d.id)
    merchants = {}
    for merchant in config.merchants:
        merchants[merchant.key] = next(
            a.id
            for a in population.accounts
            if a.is_merchant and a.merchant_category == merchant.category
        )
    rings = {r.persona_key: r.layers for r in network.rings if r.persona_key}
    payees = {
        name: merchants[story.payee] if story.payee in merchants else rings[story.payee][0][0]
        for name, story in config.stories.items()
    }
    return PersonaManifest(
        customers=customers,
        accounts=accounts,
        devices={k: tuple(v) for k, v in devices.items()},
        merchants=merchants,
        merchant_names={m.key: m.name for m in config.merchants},
        rings=rings,
        story_payees=payees,
        stories=dict(config.stories),
    )


def with_persona_fri(
    config: PersonaConfig,
    manifest: PersonaManifest,
    phones: Mapping[UUID, str | None],
    rows: tuple[PhoneRisk, ...],
) -> tuple[PhoneRisk, ...]:
    """The scripted FRI categories (Kamala's payee, the hospital: LOW) in place of the drawn
    ones. ``phones`` maps account ids to their phone numbers."""
    scripted: dict[str, str] = {}
    for ring in config.rings:
        if ring.fri is not None:
            for layer in manifest.rings[ring.key]:
                scripted.update({p: ring.fri for a in layer if (p := phones.get(a))})
    for merchant in config.merchants:
        if phone := phones.get(manifest.merchants[merchant.key]):
            scripted[phone] = merchant.fri
    return tuple(
        replace(row, category=scripted[row.phone_number]) if row.phone_number in scripted else row
        for row in rows
    )
