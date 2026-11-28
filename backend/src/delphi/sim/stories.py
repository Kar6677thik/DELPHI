"""The demo story runner, `DEMO_STORY` (docs/14 §6, §10, #101): stories S-1 and S-2.

The job stages a story's scripted payment from `sim/config/personas.yaml` for its persona, with
the ids from the dataset's persona manifest (`sim_truth`, so this runs only in the worker, as
`delphi_sim`): the persona's home account, the story payee, the amount, the channel, the
story's session telemetry and the persona's device. S-2's "new phone today" is a device of the
persona first seen at the story's T; it is keyed by the dataset, so running S-2 again before a
demo reset finds the same phone (still new, docs/08 §5).

- **autoplay**: the payment is created `PENDING_GUARDIAN` (origin `DEMO`) with its ground truth
  (the story's scenario and coaching), the story's scripted answers are registered for it with
  the simulated customer (docs/14 §5.2), and it is submitted to the worker's Guardian runner
  (runner `WORKER`, mode `AUTOPLAY`). Probes the script leaves out are answered from the
  scenario's response model.
- **interactive**: the presenter is the customer, on the mock phone, whose sessions the api's
  runner drives. The api never reads the manifest, so the job only resolves the payment (and
  stages S-2's new phone) and returns it as the `POST /guardian/sessions` body the phone pays
  with (docs/16).

Jobs run at least once, so the payment's id is keyed by the job: a retry after a lost lease
finds the payment its earlier attempt wrote, and the runner finds its session (one per
payment), instead of making a second one. Each run is one short transaction.

The story's T is the clock's now. S-1's scripted `local_time` (23:40 IST) goes on the payment
(`story_local_time`, #291), and the prior's hour features read it instead of the real time,
which is when the presenter runs it; everything else about the payment stays real.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast, get_args
from uuid import UUID, uuid5

from sqlalchemy import Connection, Engine, exists, select

from delphi.platform.clock import Clock
from delphi.platform.jobs import JobContext, JobRegistry, JobType
from delphi.sim.api import (
    RUNNABLE_STORIES,
    StoryMode,
    current_dataset,
)
from delphi.sim.bank import devices, payments
from delphi.sim.customer import AnswerStep
from delphi.sim.persona_config import PersonaConfig, Story, StoryAnswer, load_persona_config
from delphi.sim.personas import PersonaManifest, load_manifest
from delphi.sim.phone import PaymentRequest, PhoneTelemetry, initiate_payment
from delphi.sim.scenarios import SCENARIO_HYPOTHESIS
from delphi.sim.truth import payment_truth, persona_manifests, scenario_instances

# The namespace of the ids a story keys (its payment, its truth, S-2's new phone).
STORY_ID_NAMESPACE = UUID("4f0c2a52-1d8e-4b7a-9a43-6d2b5e8c1f70")
_PAISE_PER_INR = 100
_GENUINE = "GENUINE"
# The answer body of each scripted probe (docs/08 §4): an option, or a biometric result.
_BIOMETRIC_PROBES = frozenset({"BIOMETRIC_STEP_UP"})
_OPTION_PREFIXES = ("PURPOSE_QUESTION", "SPELL_BREAKER_", "COLLECT_INTENT_CHECK")


class ScriptRegistry(Protocol):
    """Where a story's scripted answers go: the worker's simulated customer."""

    def script(self, payment_id: UUID, answers: Mapping[str, Sequence[AnswerStep]]) -> None: ...


# Submits a payment to the worker's Guardian runner; returns its session's id. Idempotent: a
# payment that already has a session returns that one.
StorySubmit = Callable[[UUID], UUID]


@dataclass(frozen=True, slots=True)
class StoryParams:
    story: str
    mode: StoryMode

    @staticmethod
    def of(params: Mapping[str, Any]) -> StoryParams:
        story, mode = params.get("story"), params.get("mode")
        if story not in RUNNABLE_STORIES:
            raise ValueError(f"story must be one of {list(RUNNABLE_STORIES)}, not {story!r}")
        if mode not in get_args(StoryMode):
            raise ValueError(f"mode must be one of {list(get_args(StoryMode))}, not {mode!r}")
        return StoryParams(str(story), cast(StoryMode, mode))


def scripted_steps(answers: Sequence[StoryAnswer]) -> dict[str, list[AnswerStep]]:
    """The story's answers as the steps the simulated customer sends (docs/08 §4 bodies)."""
    steps: dict[str, list[AnswerStep]] = {}
    for a in answers:
        if a.probe in _BIOMETRIC_PROBES:
            body: dict[str, Any] = {"biometric": a.answer}
        elif a.probe.startswith(_OPTION_PREFIXES):
            body = {"option_key": a.answer}
        else:
            raise ValueError(f"a story cannot script an answer to {a.probe}")
        steps.setdefault(a.probe, []).append(AnswerStep(body, a.after_s, a.app_switches))
    return steps


@dataclass(frozen=True, slots=True)
class StagedStory:
    customer_id: UUID
    request: PaymentRequest


def _manifest(conn: Connection, config: PersonaConfig) -> tuple[UUID, PersonaManifest]:
    dataset = current_dataset(conn)
    if dataset is None:
        raise LookupError("the bank holds no dataset")
    stored = conn.execute(
        select(persona_manifests.c.manifest).where(persona_manifests.c.dataset_id == dataset.id)
    ).scalar_one_or_none()
    if stored is None:
        raise LookupError(f"dataset {dataset.id} has no personas: seed one with personas")
    return dataset.id, load_manifest(stored, config)


def _new_phone(
    conn: Connection, dataset_id: UUID, name: str, story: Story, *, customer: UUID, at: datetime
) -> UUID:
    """S-2's new phone: first seen at the story's T, once per dataset."""
    device_id = uuid5(STORY_ID_NAMESPACE, f"{dataset_id}:{name}:new-device")
    known = conn.execute(select(exists().where(devices.c.id == device_id))).scalar_one()
    if not known:
        conn.execute(
            devices.insert().values(
                id=device_id,
                customer_id=customer,
                fingerprint=f"story-{device_id}",
                os=story.new_device_os,
                first_seen_at=at,
                created_at=at,
            )
        )
    return device_id


def stage(conn: Connection, config: PersonaConfig, name: str, at: datetime) -> StagedStory:
    """The story's payment request for its persona (and S-2's new phone, staged)."""
    story = config.stories[name]
    dataset_id, manifest = _manifest(conn, config)
    customer = manifest.customers[story.customer]
    if story.new_device_os is not None:
        device = _new_phone(conn, dataset_id, name, story, customer=customer, at=at)
    else:
        device = manifest.devices[story.customer][0]  # the persona's known phone
    telemetry = PhoneTelemetry(**story.telemetry.model_dump()) if story.telemetry else None
    request = PaymentRequest(
        payer_account_id=manifest.accounts[story.customer],
        amount_paise=story.amount_inr * _PAISE_PER_INR,
        channel=story.channel,
        device_id=device,
        payee_account_id=manifest.story_payees[name],
        telemetry=telemetry,
        story_local_time=story.local_time,
    )
    return StagedStory(customer, request)


def _write_truth(conn: Connection, story: Story, name: str, payment_id: UUID, at: datetime) -> None:
    """The story payment's ground truth: what it enacts, for the simulated customer and the
    evaluation (docs/14 §10)."""
    if story.scenario is None:
        raise ValueError(f"story {name} names no scenario for its payment's truth")
    instance = uuid5(STORY_ID_NAMESPACE, f"{payment_id}:scenario")
    hypothesis = SCENARIO_HYPOTHESIS[story.scenario]
    conn.execute(
        scenario_instances.insert().values(
            id=instance,
            scenario_type=story.scenario.value,
            hypothesis_key=hypothesis,
            params={"story": name},
            started_at=at,
            created_at=at,
        )
    )
    conn.execute(
        payment_truth.insert().values(
            payment_id=payment_id,
            scenario_instance_id=instance,
            is_fraud=hypothesis != _GENUINE,
            hypothesis_key=hypothesis,
            coached=story.coached,
            created_at=at,
        )
    )


def _payment_body(request: PaymentRequest) -> dict[str, Any]:
    """The `POST /guardian/sessions` `payment` the phone pays with (docs/16)."""
    body: dict[str, Any] = {
        "payer_account_id": str(request.payer_account_id),
        "payee_account_id": str(request.payee_account_id),
        "amount_paise": request.amount_paise,
        "channel": request.channel,
        "device_id": str(request.device_id),
    }
    if request.telemetry is not None:
        body["telemetry"] = asdict(request.telemetry)
    if request.story_local_time is not None:
        body["story_local_time"] = request.story_local_time
    return body


@dataclass(frozen=True, slots=True)
class DemoStoryJob:
    sim_engine: Engine  # as delphi_sim: it reads the persona manifest and writes ground truth
    clock: Clock
    customer: ScriptRegistry
    submit: StorySubmit
    config: PersonaConfig = field(default_factory=load_persona_config)

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        params = StoryParams.of(ctx.job.params)
        story = self.config.stories[params.story]
        at = self.clock.now()
        payment_id = uuid5(STORY_ID_NAMESPACE, f"{ctx.job.id}:payment")
        with self.sim_engine.begin() as conn:
            staged = stage(conn, self.config, params.story, at)
            if params.mode == "autoplay":
                made = conn.execute(select(exists().where(payments.c.id == payment_id)))
                if not made.scalar_one():  # a retry finds the payment its first attempt made
                    initiate_payment(conn, staged.request, at, payment_id=payment_id)
                    _write_truth(conn, story, params.story, payment_id, at)
        result: dict[str, Any] = {
            "story": params.story,
            "mode": params.mode,
            "customer_id": str(staged.customer_id),
        }
        if params.mode == "interactive":
            return {**result, "payment": _payment_body(staged.request)}
        self.customer.script(payment_id, scripted_steps(story.answers))
        session_id = self.submit(payment_id)
        return {**result, "payment_id": str(payment_id), "session_id": str(session_id)}


def register_story_job(
    registry: JobRegistry,
    *,
    sim_engine: Engine,
    clock: Clock,
    customer: ScriptRegistry,
    submit: StorySubmit,
) -> None:
    """Add the `DEMO_STORY` handler. The worker's composition root wires ``submit`` to its
    Guardian runner and ``customer`` to the simulated customer that runner's channel asks."""
    registry.register(
        JobType.DEMO_STORY,
        DemoStoryJob(sim_engine=sim_engine, clock=clock, customer=customer, submit=submit),
    )
