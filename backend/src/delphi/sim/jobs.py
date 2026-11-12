"""The simulator's job handlers (docs/04 §5.4, #19) and their registration.

``delphi.platform`` may not import feature modules (import-linter), so the worker's composition
root (``delphi.cli``) builds the ``delphi_sim`` engine and calls ``register_sim_jobs``.

**GENERATE_DATASET** generates a profile in memory, then writes it into the bank in one
``delphi_sim`` transaction: the bank and ground-truth rows (``delphi.sim.writer``), the
``delphi.datasets`` row with ``seed_completed_at`` (the marker demo reset deletes back to,
docs/14 §10), and the persona manifest (docs/14 §6). Jobs run at least once, so the handler is
idempotent: a retry finds the dataset its earlier attempt committed (by ``job_id``) and returns
it. The bank holds one dataset; a second one is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, exists, func, select

from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import Clock
from delphi.platform.ids import uuid7
from delphi.platform.jobs import JobContext, JobRegistry, JobType, enqueue
from delphi.platform.locks import DATASET_WRITE_LOCK_KEY
from delphi.sim import bank
from delphi.sim.catalog import READY, datasets
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.personas import manifest_json
from delphi.sim.response_models import load_config
from delphi.sim.stats import StatsReport, dataset_stats
from delphi.sim.truth import persona_manifests
from delphi.sim.writer import write_dataset

# Progress reported as the job goes (docs/16 §2): started, generated, written.
PROGRESS_STARTED = 0.05
PROGRESS_GENERATED = 0.6
PROGRESS_WRITTEN = 0.95


class DatasetExistsError(RuntimeError):
    """The bank already holds a dataset (or rows from elsewhere)."""


class LeaseLostError(RuntimeError):
    """The job is no longer this worker's, so its result must not be written."""


@dataclass(frozen=True, slots=True)
class DatasetParams:
    """A ``GENERATE_DATASET`` job's parameters: the profile, an optional seed in place of the
    profile's, and whether to add the personas (default: the profiles `personas.yaml` lists)."""

    profile: str
    seed: int | None = None
    personas: bool | None = None

    @staticmethod
    def of(params: Mapping[str, Any], config: GeneratorConfig) -> DatasetParams:
        profile = params.get("profile")
        if profile not in config.profiles:
            raise ValueError(f"unknown profile {profile!r}; use one of {sorted(config.profiles)}")
        seed = params.get("seed")
        if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or seed < 0):
            raise ValueError(f"seed must be a non-negative integer, not {seed!r}")
        personas = params.get("personas")
        if personas is not None and not isinstance(personas, bool):
            raise ValueError(f"personas must be true or false, not {personas!r}")
        return DatasetParams(profile, seed, personas)

    def config(self, config: GeneratorConfig) -> GeneratorConfig:
        """``config`` with this job's seed for its profile."""
        if self.seed is None:
            return config
        profile = config.profiles[self.profile].model_copy(update={"seed": self.seed})
        return config.model_copy(update={"profiles": {**config.profiles, self.profile: profile}})


def _report_json(report: StatsReport) -> dict[str, Any]:
    data = asdict(report)
    data["rates"] = [{**r, "scenario": r["scenario"].value} for r in data["rates"]]
    return data


def _summary(row: Mapping[Any, Any]) -> dict[str, Any]:
    return {
        "dataset_id": str(row["id"]),
        "profile": row["profile"],
        "seed": row["seed"],
        "generator_version": row["generator_version"],
        "rows": row["stats"]["rows"],
    }


def _committed(conn: Connection, job_id: UUID) -> dict[str, Any] | None:
    """This job's dataset if an earlier attempt committed it. Raises if the bank holds anything
    else, since the bank tables hold one dataset (their ids are deterministic)."""
    row = conn.execute(select(datasets).where(datasets.c.job_id == job_id)).mappings().first()
    if row is not None:
        return _summary(row)
    other = conn.execute(select(datasets.c.id, datasets.c.profile).limit(1)).first()
    if other is not None:
        raise DatasetExistsError(
            f"the database already holds dataset {other.id} ({other.profile}); reset it first"
        )
    if conn.execute(select(exists().select_from(bank.customers))).scalar_one():
        raise DatasetExistsError("the bank already holds rows that no dataset accounts for")
    return None


@dataclass(frozen=True, slots=True)
class GenerateDatasetJob:
    sim_engine: Engine  # as delphi_sim: it writes ground truth
    clock: Clock
    kb: KnowledgeBase
    config: GeneratorConfig
    generator_version: int
    # Profiles this deployment may generate (DELPHI_DATASET_PROFILES, docs/23 §4).
    allowed_profiles: frozenset[str] | None = None

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        params = DatasetParams.of(ctx.job.params, self.config)
        if self.allowed_profiles is not None and params.profile not in self.allowed_profiles:
            raise ValueError(f"profile {params.profile!r} is turned off on this deployment")
        with self.sim_engine.connect() as conn:
            done = _committed(conn, ctx.job.id)
        if done is not None:
            return self._then_train(ctx, done)
        self._report(ctx, PROGRESS_STARTED)
        config = params.config(self.config)
        dataset = generate_dataset(
            config,
            params.profile,
            kb=self.kb,
            created_at=self.clock.now(),  # `demo` ends its history now (docs/14 §2)
            personas=params.personas,
        )
        report = _report_json(dataset_stats(dataset, config))
        self._report(ctx, PROGRESS_GENERATED)
        with self.sim_engine.begin() as conn:
            conn.execute(select(func.pg_advisory_xact_lock(DATASET_WRITE_LOCK_KEY)))
            done = _committed(conn, ctx.job.id)
            if done is not None:
                return self._then_train(ctx, done)
            rows = dict(write_dataset(conn, dataset))
            now = self.clock.now()
            dataset_id = uuid7(unix_ms=int(now.timestamp() * 1000))
            row = {
                "id": dataset_id,
                "job_id": ctx.job.id,
                "profile": params.profile,
                "seed": config.profiles[params.profile].seed,
                "generator_version": self.generator_version,
                "status": READY,
                "stats": {"rows": rows, "report": report},
                "seed_completed_at": now,
                "created_at": now,
            }
            conn.execute(datasets.insert().values(**row))
            if dataset.personas is not None:
                conn.execute(
                    persona_manifests.insert().values(
                        dataset_id=dataset_id,
                        manifest=manifest_json(dataset.personas),
                        created_at=now,
                    )
                )
            self._report(ctx, PROGRESS_WRITTEN)  # raising here rolls the write back
        return self._then_train(ctx, _summary(row))

    def _then_train(self, ctx: JobContext, summary: dict[str, Any]) -> dict[str, Any]:
        """Queue ``TRAIN_SENSING`` on the dataset just written (docs/13 §7: the seed job trains
        once after generation). The key names the dataset, so a retried job queues it once."""
        with ctx.queue.sessions.begin() as session:
            queued = enqueue(
                session,
                self.clock,
                JobType.TRAIN_SENSING,
                idempotency_key=f"TRAIN_SENSING:dataset-{summary['dataset_id']}",
                params={"dataset_id": summary["dataset_id"]},
            )
        return {**summary, "train_job_id": str(queued.job_id)}

    @staticmethod
    def _report(ctx: JobContext, progress: float) -> None:
        if not ctx.report(progress):
            raise LeaseLostError(f"job {ctx.job.id} is no longer this worker's")


def register_sim_jobs(
    registry: JobRegistry,
    *,
    sim_engine: Engine,
    clock: Clock,
    kb: KnowledgeBase,
    allowed_profiles: frozenset[str] | None = None,
) -> None:
    """Add the simulator's handlers. ``sim_engine`` connects as ``delphi_sim``."""
    registry.register(
        JobType.GENERATE_DATASET,
        GenerateDatasetJob(
            sim_engine=sim_engine,
            clock=clock,
            kb=kb,
            config=load_generator_config(),
            generator_version=load_config().generator_version,
            allowed_profiles=allowed_profiles,
        ),
    )
