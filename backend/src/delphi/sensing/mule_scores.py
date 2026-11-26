"""Mule scores in use (docs/13 §6, FR-SNS-05, #91): the ``MULE_SWEEP`` job that scores every
account into ``bank.account_scores``, and ``MuleScores``, which serves ``p_mule`` on demand.

Both read the `bank` tables through ``delphi.sim.api`` as ``delphi_app``, never ``sim_truth``,
and use the ACTIVE ``MULE`` artifact. ``MuleScores`` implements the probes' ``MuleScorePort``
(``BENEFICIARY_INTEL``) and gives the Guardian the payer's mule score (``with_payer_mule``).

- **The sweep** (every minute) rescores, as of the clock, the accounts with no score or one
  older than ``mule_sweep_interval_min``, including other banks' (A-06), from each account's
  online snapshot. Bounded as in #263: accounts chosen in SQL in key order, one transaction per
  batch with ``SET LOCAL`` limits and a skip-when-busy advisory lock, and a per-run cap; the
  next run carries on. A rerun rewrites the same rows. With no ACTIVE ``MULE`` model it scores
  nothing and says why.
- **On demand:** a cached score counts only if it was computed before ``as_of`` (a score the
  bank did not have yet must not leak into an earlier decision). Otherwise the account is
  scored as of ``as_of`` from its online snapshot. If that fails, or there is no model,
  ``p_mule`` is ``mule_score_fallback`` (0.10), logged as a WARNING.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import Connection, Engine, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert

from delphi.kb.store import active_version
from delphi.platform.clock import Clock
from delphi.platform.jobs import JobContext
from delphi.platform.locks import MULE_SWEEP_LOCK_KEY
from delphi.platform.logging import get_logger
from delphi.platform.settings import Settings
from delphi.sensing.artifacts import ArtifactKind, StoredArtifact, active_artifacts, load_blob
from delphi.sensing.config import SensingConfig, load_sensing_config
from delphi.sensing.features import ACCOUNT_FEATURES, build_account_features, to_us
from delphi.sensing.history import cached_mule_score, load_account_snapshot
from delphi.sensing.mule import MuleModel
from delphi.sim import api as bank

log = get_logger(__name__)

STATEMENT_TIMEOUT = "30s"  # per statement in a sweep batch, as in #263
WORK_MEM = "16MB"


def active_mule_model(conn: Connection) -> tuple[StoredArtifact, MuleModel] | None:
    """The ACTIVE ``MULE`` artifact and its model, or ``None`` before one is trained."""
    artifact = active_artifacts(conn).get(ArtifactKind.MULE)
    if artifact is None:
        return None
    return artifact, MuleModel.from_bytes(load_blob(conn, artifact.id))


def upsert_scores(
    conn: Connection, scores: Mapping[str, float], computed_at: datetime, artifact_id: UUID
) -> None:
    s = bank.account_scores
    if not scores:
        return
    statement = insert(s).values(
        [
            {
                "account_id": UUID(account),
                "p_mule": float(p),
                "computed_at": computed_at,
                "model_artifact_id": artifact_id,
            }
            for account, p in scores.items()
        ]
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[s.c.account_id],
            set_={
                "p_mule": statement.excluded.p_mule,
                "computed_at": statement.excluded.computed_at,
                "model_artifact_id": statement.excluded.model_artifact_id,
            },
        )
    )


@dataclass(frozen=True, slots=True)
class MuleSweepLimits:
    """How much one ``MULE_SWEEP`` run does (settings ``mule_sweep_*``, #252)."""

    batch: int  # accounts scored per transaction
    max_per_run: int  # accounts scored per run

    @staticmethod
    def of(settings: Settings) -> MuleSweepLimits:
        return MuleSweepLimits(
            batch=settings.mule_sweep_batch, max_per_run=settings.mule_sweep_max_per_run
        )


def stale_accounts(
    conn: Connection, *, before: datetime, after: UUID | None, limit: int
) -> list[UUID]:
    """Accounts with no stored score, or one computed at or before ``before``, in key order
    after ``after``: at most ``limit``."""
    a, s = bank.accounts.c, bank.account_scores.c
    fresh = exists().where(s.account_id == a.id, s.computed_at > before)
    query = select(a.id).where(~fresh)
    if after is not None:
        query = query.where(a.id > after)
    return list(conn.execute(query.order_by(a.id).limit(limit)).scalars())


def _begin_batch(conn: Connection) -> bool:
    """Bound this transaction and take the sweep lock; False if another run holds it."""
    conn.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    conn.execute(text(f"SET LOCAL work_mem = '{WORK_MEM}'"))
    return bool(conn.execute(select(func.pg_try_advisory_xact_lock(MULE_SWEEP_LOCK_KEY))).scalar())


def score_accounts(
    conn: Connection,
    model: MuleModel,
    accounts: list[UUID],
    now: datetime,
    settings: SensingConfig,
) -> dict[str, float]:
    """Each account's score as of ``now``, from its online snapshot (docs/13 §6)."""
    as_of = to_us(now)
    matrix = np.array(
        [
            [
                build_account_features(
                    load_account_snapshot(conn, account, now, settings.accounts),
                    as_of,
                    settings.accounts,
                )[f]
                for f in model.feature_list
            ]
            for account in accounts
        ],
        dtype=np.float64,
    ).reshape(len(accounts), len(model.feature_list))
    scores = model.scores(matrix) if accounts else np.empty(0)
    return {str(a): float(p) for a, p in zip(accounts, scores, strict=True)}


def sweep_mule_scores(
    engine: Engine,
    clock: Clock,
    limits: MuleSweepLimits,
    *,
    settings: SensingConfig | None = None,
    demo_time: bool = False,
    report: Callable[[float], bool] = lambda _fraction: True,
) -> dict[str, Any]:
    """docs/13 §6: rescore, as of the clock, the accounts whose score is missing or older than
    ``mule_sweep_interval_min`` (time-scaled), in key order. One transaction per batch, each
    bounded and under the sweep lock (skipped while another run holds it), at most
    ``limits.max_per_run`` accounts per run; the next run carries on."""
    settings = settings or load_sensing_config()
    now = clock.now()
    scored = 0
    after: UUID | None = None
    artifact_id: UUID | None = None

    def result(**extra: Any) -> dict[str, Any]:
        found = {"model_artifact_id": str(artifact_id)} if artifact_id is not None else {}
        return {"scored": scored, **found, "computed_at": now.isoformat(), **extra}

    while scored < limits.max_per_run:
        with engine.begin() as conn:
            if not _begin_batch(conn):
                return result(skipped="busy")
            version = active_version(conn)
            active = active_mule_model(conn)
            if version is None or active is None:
                reason = "no ACTIVE MULE model" if active is None else "no ACTIVE KB version"
                log.warning("sensing.mule_sweep_skipped", reason=reason)
                return result(skipped=reason)
            artifact, model = active
            artifact_id = artifact.id
            config = version.kb.config
            scale = config.time.demo_time_scale if demo_time else 1.0
            before = now - timedelta(minutes=config.navigator.mule_sweep_interval_min * scale)
            room = min(limits.batch, limits.max_per_run - scored)
            accounts = stale_accounts(conn, before=before, after=after, limit=room)
            if not accounts:
                return result(more=False)
            upsert_scores(
                conn, score_accounts(conn, model, accounts, now, settings), now, artifact.id
            )
        scored += len(accounts)
        after = accounts[-1]
        if not report(scored / limits.max_per_run):
            break
    return result(more=True)


@dataclass(frozen=True, slots=True)
class MuleSweepJob:
    """``MULE_SWEEP`` (every minute, docs/04 §5.4). Time runs REAL until the DEMO time mode is
    wired, as for the Guardian's and the Navigator's jobs."""

    engine: Engine  # as delphi_app
    clock: Clock
    limits: MuleSweepLimits

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        return sweep_mule_scores(self.engine, self.clock, self.limits, report=ctx.report)


@dataclass(slots=True)
class MuleScores:
    """``p_mule(account, as_of)`` for the probes' ``MuleScorePort`` and the Guardian prior."""

    engine: Engine  # as delphi_app
    clock: Clock
    settings: SensingConfig = field(default_factory=load_sensing_config)
    _model: tuple[UUID, MuleModel] | None = None

    def _active(self, conn: Connection) -> MuleModel | None:
        artifact = active_artifacts(conn).get(ArtifactKind.MULE)
        if artifact is None:
            return None
        if self._model is None or self._model[0] != artifact.id:
            self._model = (artifact.id, MuleModel.from_bytes(load_blob(conn, artifact.id)))
        return self._model[1]

    def p_mule(self, account_id: UUID, as_of: datetime) -> float:
        fallback = self.settings.features.mule_score_fallback
        try:
            with self.engine.begin() as conn:
                cached = cached_mule_score(conn, account_id, as_of)
                if cached is not None:
                    return cached
                model = self._active(conn)
                if model is None:
                    log.warning(
                        "sensing.mule_score_fallback", account_id=str(account_id), reason="no model"
                    )
                    return fallback
                snapshot = load_account_snapshot(conn, account_id, as_of, self.settings.accounts)
                features = build_account_features(snapshot, to_us(as_of), self.settings.accounts)
                p = model.p_mule({f: features[f] for f in ACCOUNT_FEATURES})
                s = bank.account_scores
                if not conn.execute(
                    select(s.c.account_id).where(s.c.account_id == account_id)
                ).first():
                    # An account with no score yet (e.g. just opened) keeps its on-demand score.
                    assert self._model is not None
                    upsert_scores(conn, {str(account_id): p}, as_of, self._model[0])
                return p
        except Exception as exc:  # docs/13 §6: a failed score falls back, it never raises
            log.warning("sensing.mule_score_fallback", account_id=str(account_id), reason=repr(exc))
            return fallback
