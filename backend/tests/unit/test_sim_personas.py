"""The demo personas (docs/14 §6, docs/21 §3 ENG-S1/S2/S3, #19)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, time, timedelta
from uuid import UUID

import pytest

from delphi.kb.model import ClassifierThresholds, KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.export import canonical_export
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.history import IST
from delphi.sim.persona_config import PersonaConfig, Story, StoryTelemetry, load_persona_config
from delphi.sim.personas import load_manifest, manifest_json

from ..golden.test_story_behaviour import ARJUN_PASSIVE, KAMALA_PASSIVE


@pytest.fixture(scope="module")
def config() -> GeneratorConfig:
    return load_generator_config()


@pytest.fixture(scope="module")
def dataset(config: GeneratorConfig, kb: KnowledgeBase) -> Dataset:
    """The `test` profile with the personas, as the E2E stack runs it (docs/21 §1)."""
    return generate_dataset(config, "test", kb=kb, personas=True)


def _story_time(dataset: Dataset, story: Story) -> datetime:
    """The story's T: the first day after the history, at the scripted local time."""
    day = (dataset.population.history_end + timedelta(days=1)).astimezone(IST).date()
    hours, minutes = (int(x) for x in (story.local_time or "12:00").split(":"))
    return datetime.combine(day, time(hours, minutes), tzinfo=IST)


def _passive(  # docs/08 §5, first match wins
    dataset: Dataset,
    *,
    payee: UUID,
    device_first_seen: datetime,
    telemetry: StoryTelemetry,
    at: datetime,
    th: ClassifierThresholds,
) -> dict[str, str]:
    history = dataset.history
    listed = {r.account_id for r in (*dataset.network.listings, *history.registry_listings)}
    accounts = {a.id: a for a in (*dataset.population.accounts, *dataset.network.accounts)}
    owner = {a.id: a.customer_id for a in accounts.values()}
    earlier = [p for p in history.payments if p.payee_account_id == payee and p.initiated_at < at]
    payers = {owner.get(p.payer_account_id) for p in earlier}
    span = (
        max(p.initiated_at for p in earlier) - min(p.initiated_at for p in earlier)
        if earlier
        else timedelta(0)
    )
    established = (
        len(earlier) >= th.established_payee_min_payments
        and len(payers) >= th.established_payee_min_distinct_customers
        and span >= timedelta(days=th.established_payee_min_age_days)
    )
    if payee in listed:
        beneficiary = "LISTED_SUSPECT"
    elif accounts[payee].is_merchant or established:
        beneficiary = "CLEAN_KNOWN_ENTITY"
    else:
        beneficiary = "CLEAN_UNKNOWN"  # p_mule < 0.5 is the M3 scorer's (docs/tech-debt TD-9)
    fri = {r.phone_number: r.category for r in dataset.phone_risk}
    category = fri.get(accounts[payee].phone_number or "", "LOW")
    phone = {"VERY_HIGH": "VERY_HIGH", "HIGH": "MEDIUM_HIGH", "MEDIUM": "MEDIUM_HIGH"}.get(
        category, "LOW_OR_UNKNOWN"
    )
    if telemetry.remote_access_active or telemetry.screen_share_active:
        device = "REMOTE_ACCESS_ACTIVE"
    elif at - device_first_seen < timedelta(days=th.new_device_days):
        device = "NEW_DEVICE"
    else:
        device = "KNOWN_DEVICE"
    if telemetry.paste_on_account_field or (
        telemetry.max_field_dwell_s > th.paste_or_dwell_max_field_dwell_s
    ):
        session = "HESITANT_OR_PASTED"
    elif telemetry.typing_cadence_z > th.cadence_mismatch_z:
        session = "CADENCE_MISMATCH"
    else:
        session = "NORMAL"
    if telemetry.on_call and telemetry.call_counterparty_known is False:
        call = "ON_CALL_UNKNOWN_NUMBER"
    elif telemetry.on_call:
        call = "ON_CALL_KNOWN_CONTACT"
    else:
        call = "NOT_ON_CALL"
    return {
        "BENEFICIARY_INTEL": beneficiary,
        "COUNTERPARTY_PHONE_RISK": phone,
        "DEVICE_POSTURE": device,
        "SESSION_BEHAVIOUR": session,
        "CALL_STATUS": call,
    }


def test_the_personas_are_who_docs_14_section_6_says(dataset: Dataset) -> None:
    pop, manifest = dataset.population, dataset.personas
    assert manifest is not None
    customers = {c.persona_key: c for c in pop.customers if c.persona_key}
    assert len(customers) == 3 + 15
    kamala, arjun, ravi = customers["KAMALA"], customers["ARJUN"], customers["RAVI"]
    assert (kamala.display_name, kamala.age, kamala.language, kamala.consent_call_status) == (
        "Kamala Devi",
        68,
        "te",
        True,
    )
    assert not kamala.is_vulnerable  # 68 < 70 and no disability (docs/05 §1)
    assert (arjun.age, arjun.language, ravi.age, ravi.language) == (34, "en", 45, "hi")
    assert len(manifest.devices["KAMALA"]) == 1  # "1 device"
    account = {a.id: a for a in pop.accounts}[manifest.accounts["KAMALA"]]
    tenure = (pop.history_end - account.opened_at).days / 365
    assert tenure == pytest.approx(6, abs=0.01)  # "6 years as a customer"
    victims = [k for k in customers if k.startswith("S4_VICTIM_")]
    assert sorted(victims) == [f"S4_VICTIM_{n:02d}" for n in range(1, 16)]


def test_no_fraud_reaches_a_persona_before_its_story(dataset: Dataset) -> None:
    """docs/14 §8: the S-4 victims have no history of the scam; the stories happen live."""
    manifest, history = dataset.personas, dataset.history
    assert manifest is not None
    persona_accounts = set(manifest.accounts.values())
    fraud = {t.payment_id for t in history.payment_truth if t.is_fraud}
    assert not any(
        p.payer_account_id in persona_accounts for p in history.payments if p.id in fraud
    )
    ring_accounts = {a for layers in manifest.rings.values() for layer in layers for a in layer}
    assert not any(p.payee_account_id in ring_accounts for p in history.payments)
    assert not any(p.payer_account_id in ring_accounts for p in history.payments)
    listed = {r.account_id for r in (*dataset.network.listings, *history.registry_listings)}
    assert not listed & ring_accounts
    labelled = {label.subject_id for label in history.account_labels}
    assert not labelled & {str(a) for a in ring_accounts}


def test_kamalas_payee_and_ring_7(dataset: Dataset) -> None:
    manifest, network, pop = dataset.personas, dataset.network, dataset.population
    assert manifest is not None
    accounts = {a.id: a for a in network.accounts}
    truth = {t.account_id: t for t in dataset.account_truth}
    (payee,) = manifest.rings["KAMALA_PAYEE"][0]
    mule = accounts[payee]
    assert manifest.story_payees["S1"] == payee
    assert mule.bank_code == "XBK2" and mule.customer_id is None and mule.kyc_level == "FULL"
    assert pop.history_end - mule.opened_at == timedelta(days=3)  # freshly opened
    assert truth[payee].is_mule and truth[payee].layer == 1
    ring7 = manifest.rings["RING_7"]
    assert [len(layer) for layer in ring7] == [4, 4, 4]  # 12 accounts, 3 layers
    banks = Counter(accounts[a].bank_code for layer in ring7 for a in layer)
    assert set(banks) == {"DLPH", "XBK1", "XBK3"}
    for layer, ids in enumerate(ring7, start=1):
        assert all(truth[a].is_mule and truth[a].layer == layer for a in ids)
    home = [accounts[a] for layer in ring7 for a in layer if accounts[a].bank_code == "DLPH"]
    devices = {d.customer_id: d.fingerprint for d in network.devices}
    assert len({devices[a.customer_id] for a in home}) == 1  # one shared phone
    assert manifest.story_payees["S3"] == ring7[0][0]
    sunrise = {a.id: a for a in pop.accounts}[manifest.merchants["SUNRISE_HOSPITAL"]]
    assert sunrise.is_merchant and sunrise.merchant_category == "HOSPITAL"
    assert manifest.merchant_names["SUNRISE_HOSPITAL"] == "Sunrise Multispeciality Hospital"


def test_kamalas_passive_outcomes_are_the_golden_ones(dataset: Dataset, kb: KnowledgeBase) -> None:
    """docs/14 §6 / ENG-S1: CLEAN_UNKNOWN, LOW_OR_UNKNOWN, KNOWN_DEVICE, NORMAL,
    ON_CALL_UNKNOWN_NUMBER, from the generated rows and the S-1 script."""
    manifest = dataset.personas
    assert manifest is not None
    story = manifest.stories["S1"]
    assert story.telemetry is not None
    (device,) = manifest.devices["KAMALA"]
    first_seen = {d.id: d.first_seen_at for d in dataset.population.devices}[device]
    outcomes = _passive(
        dataset,
        payee=manifest.story_payees["S1"],
        device_first_seen=first_seen,
        telemetry=story.telemetry,
        at=_story_time(dataset, story),
        th=kb.config.classifiers,
    )
    assert outcomes == KAMALA_PASSIVE


def test_arjuns_passive_outcomes_are_the_golden_ones(dataset: Dataset, kb: KnowledgeBase) -> None:
    """docs/06 §17 / ENG-S2: CLEAN_KNOWN_ENTITY, LOW_OR_UNKNOWN, NEW_DEVICE (the phone first
    seen at T), NORMAL, NOT_ON_CALL."""
    manifest = dataset.personas
    assert manifest is not None
    story = manifest.stories["S2"]
    assert story.telemetry is not None and story.new_device_os is not None
    at = _story_time(dataset, story)
    outcomes = _passive(
        dataset,
        payee=manifest.story_payees["S2"],
        device_first_seen=at,
        telemetry=story.telemetry,
        at=at,
        th=kb.config.classifiers,
    )
    assert outcomes == ARJUN_PASSIVE


def test_the_story_scripts(dataset: Dataset) -> None:
    """The amounts, times and answers docs/14 §6 and docs/21 ENG-S1/S2/S3 script, and every
    persona can afford its story at the end of the history."""
    manifest = dataset.personas
    assert manifest is not None
    s1, s2, s3 = (manifest.stories[k] for k in ("S1", "S2", "S3"))
    assert (s1.amount_inr, s1.local_time) == (480_000, "23:40")
    assert [(a.probe, a.answer, a.after_s, a.app_switches) for a in s1.answers] == [
        ("PURPOSE_QUESTION", "FAMILY_OR_FRIEND", 6, 0),
        ("SPELL_BREAKER_DIGITAL_ARREST", "NO", 11, 1),
    ]
    assert s2.amount_inr == 180_000
    assert ("BIOMETRIC_STEP_UP", "MATCH") in [(a.probe, a.answer) for a in s2.answers]
    assert ("PURPOSE_QUESTION", "MEDICAL", 5) in [
        (a.probe, a.answer, a.after_s) for a in s2.answers
    ]
    assert (s3.amount_inr, s3.minutes_before_t0, s3.complaint_channel) == (300_000, 10, "NCRP_1930")
    for story in (s1, s2, s3):
        balance = dataset.history.balances[manifest.accounts[story.customer]]
        assert balance >= story.amount_inr * 100


def test_personas_change_no_other_row(config: GeneratorConfig, kb: KnowledgeBase) -> None:
    """Personas come after every generated row, so `test` without them is unchanged, and the
    manifest is the same on every run."""
    plain = generate_dataset(config, "test", kb=kb)
    assert plain.personas is None
    with_personas = generate_dataset(config, "test", kb=kb, personas=True)
    n = len(plain.population.customers)
    assert with_personas.population.customers[:n] == plain.population.customers
    assert with_personas.network.rings[: len(plain.network.rings)] == plain.network.rings
    again = generate_dataset(config, "test", kb=kb, personas=True)
    assert again.personas == with_personas.personas
    assert canonical_export(again) == canonical_export(with_personas)


@pytest.mark.parametrize(
    "created_at", ["2026-11-07T00:00:00Z", "2027-03-15T09:30:00Z", "2027-08-01T18:00:00Z"]
)
def test_persona_devices_are_stable_on_demo(
    config: GeneratorConfig, kb: KnowledgeBase, created_at: str
) -> None:
    """#57: personas get no new phones in the history, so Kamala, Arjun and Ravi keep their one
    device whenever `demo` is created (fewer customers here, to keep it quick)."""
    small = config.profiles["demo"].model_copy(update={"customers": 100})
    smaller = config.model_copy(update={"profiles": {**config.profiles, "demo": small}})
    at = datetime.fromisoformat(created_at)
    dataset = generate_dataset(smaller, "demo", kb=kb, created_at=at)
    assert dataset.personas is not None
    personas = {c.id for c in dataset.population.customers if c.persona_key}
    assert not [d for d in dataset.history.devices if d.customer_id in personas]
    assert dataset.history.devices  # the generated customers still get new phones
    for key in ("KAMALA", "ARJUN", "RAVI"):
        assert len(dataset.personas.devices[key]) == 1, key


def test_the_demo_profile_has_personas_by_default() -> None:
    assert "demo" in load_persona_config().profiles


def test_the_manifest_survives_its_json_form(dataset: Dataset) -> None:
    """#19: the GENERATE_DATASET job stores the manifest as JSON; the runners load it back."""
    manifest = dataset.personas
    assert manifest is not None
    stored = json.loads(json.dumps(manifest_json(manifest)))
    assert "stories" not in stored  # personas.yaml's, not the dataset's
    assert load_manifest(stored, load_persona_config()) == manifest


def test_the_persona_config_rejects_broken_references() -> None:
    data = load_persona_config().model_dump()
    bad_story = {
        **data,
        "stories": {**data["stories"], "S9": {**data["stories"]["S1"], "payee": "NOBODY"}},
    }
    with pytest.raises(ValueError, match="unknown persona"):
        PersonaConfig.model_validate(bad_story)
    duplicate = {
        **data,
        "merchants": [*data["merchants"], {**data["merchants"][0], "key": "KAMALA"}],
    }
    with pytest.raises(ValueError, match="unique"):
        PersonaConfig.model_validate(duplicate)
    empty = {**data, "rings": [{**data["rings"][0], "layers": [[]]}]}
    with pytest.raises(ValueError, match="every layer"):
        PersonaConfig.model_validate(empty)
