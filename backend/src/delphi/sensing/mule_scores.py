"""Mule scores in use (docs/13 §6, FR-SNS-05, #91): the ``MULE_SWEEP`` job that scores every
account into ``bank.account_scores``, and ``MuleScores``, which serves ``p_mule`` on demand.

Both read the `bank` tables through ``delphi.sim.api`` as ``delphi_app``, never ``sim_truth``,
and use the ACTIVE ``MULE`` artifact. ``MuleScores`` implements the probes' ``MuleScorePort``
(``BENEFICIARY_INTEL``) and gives the Guardian the payer's mule score (``with_payer_mule``).

- **The sweep** scores all accounts, including other banks' (A-06), as of the clock, and
  upserts one row per account. A rerun for the same slot rewrites the same rows, so the job is
  idempotent. With no ACTIVE ``MULE`` model it scores nothing and says why.
- **On demand:** a cached score counts only if it was computed before ``as_of`` (a score the
  bank did not have yet must not leak into an earlier decision). Otherwise the account is
  scored as of ``as_of`` from its online snapshot. If that fails, or there is no model,
  ``p_mule`` is ``mule_score_fallback`` (0.10), logged as a WARNING.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import Connection, Engine, select
from sqlalchemy.dialects.postgresql import insert

from delphi.platform.clock import Clock
from delphi.platform.jobs import JobContext
from delphi.platform.logging import get_logger
from delphi.sensing.artifacts import ArtifactKind, StoredArtifact, active_artifacts, load_blob
from delphi.sensing.config import SensingConfig, load_sensing_config
from delphi.sensing.features import ACCOUNT_FEATURES, build_account_features, to_us
from delphi.sensing.history import cached_mule_score, load_account_snapshot
from delphi.sensing.mule import MuleModel
from delphi.sensing.offline import OfflineAccountHistories, load_bank_frames
from delphi.sim import api as bank

log = get_logger(__name__)


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


def sweep_mule_scores(
    engine: Engine, clock: Clock, settings: SensingConfig | None = None
) -> dict[str, Any]:
    """Score every account as of the clock and store the scores (docs/13 §6)."""
    settings = settings or load_sensing_config()
    now = clock.now()
    with engine.begin() as conn:
        active = active_mule_model(conn)
        if active is None:
            log.warning("sensing.mule_sweep_skipped", reason="no ACTIVE MULE model")
            return {"scored": 0, "skipped": "no ACTIVE MULE model"}
        artifact, model = active
        frames = load_bank_frames(conn)
        offline = OfflineAccountHistories(frames, settings.accounts)
        accounts = [str(a) for a in frames.accounts["id"]]
        as_of = to_us(now)
        matrix = np.array(
            [
                [
                    build_account_features(offline.snapshot(a), as_of, settings.accounts)[f]
                    for f in model.feature_list
                ]
                for a in accounts
            ],
            dtype=np.float64,
        ).reshape(len(accounts), len(model.feature_list))
        scores = model.scores(matrix) if accounts else np.empty(0)
        upsert_scores(conn, dict(zip(accounts, scores, strict=True)), now, artifact.id)
    return {
        "scored": len(accounts),
        "model_artifact_id": str(artifact.id),
        "computed_at": now.isoformat(),
    }


@dataclass(frozen=True, slots=True)
class MuleSweepJob:
    engine: Engine  # as delphi_app
    clock: Clock

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        return sweep_mule_scores(self.engine, self.clock)


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
