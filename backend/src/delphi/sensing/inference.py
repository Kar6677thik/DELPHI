"""The online sensing inference (docs/13 §1, §7, #92): what the sensing layer says about one
payment, for the Guardian prior (``PriorPort``), and ``p_mule(account)`` for its other consumers.

For a payment it loads the payer's history snapshot as of the payment's initiation
(``delphi.sensing.history``) and builds the features with the same function training uses
(``build_payment_features``, docs/13 §2), so serving cannot drift from training. Then:

- ``p_payment``: the ACTIVE ``PRIOR`` model's calibrated probabilities (docs/13 §4);
- ``top_features``: its top ``pred_contrib`` entries for the most probable class, each
  ``{"feature", "value", "contribution"}`` (docs/13 §1);
- ``anomaly_percentile``: ``q`` from the ACTIVE ``ANOMALY`` model (docs/13 §5);
- ``p_mule_payer``: the payer account's mule score as of the initiation (``MuleScores``,
  docs/13 §6), or ``None`` without an ACTIVE ``MULE`` model, so ``with_payer_mule`` leaves
  MULE to the KB's open mass (docs/06 §11).

It reads the `bank` tables through ``delphi.sim.api`` as ``delphi_app``, never ``sim_truth``.
Without an ACTIVE ``PRIOR`` or ``ANOMALY`` artifact it raises ``NOT_INITIALISED``.

**Reload.** The loaded models are cached by artifact id. Every call reads the ACTIVE ids (one
small query on ``model_artifacts``) and loads a blob only when an id changed, so an artifact
activated by any process (``artifact.activated``) is served from the next call, without a
restart. ``refresh()`` loads them up front: call it at api/worker start.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import Connection, Engine

from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.sensing.anomaly import AnomalyModel, FlatForest
from delphi.sensing.artifacts import ArtifactKind, active_artifact_ids, load_blob
from delphi.sensing.config import SensingConfig, load_sensing_config
from delphi.sensing.features import build_payment_features
from delphi.sensing.history import cached_mule_score, load_payment_snapshot
from delphi.sensing.mule_scores import MuleScores
from delphi.sensing.prior import PriorModel
from delphi.sim import api as bank


@dataclass(frozen=True, slots=True, eq=False)
class _Anomaly:
    model: AnomalyModel
    flat: FlatForest

    @staticmethod
    def load(blob: bytes) -> _Anomaly:
        model = AnomalyModel.from_bytes(blob)
        return _Anomaly(model, FlatForest.of(model.forest))

    def percentile(self, features: Mapping[str, float]) -> float:
        row = np.array([features[f] for f in self.model.feature_list], dtype=np.float64)
        return self.model.percentile_of_score(self.flat.score(row))


_LOADERS: dict[ArtifactKind, Callable[[bytes], Any]] = {
    ArtifactKind.PRIOR: PriorModel.from_bytes,
    ArtifactKind.ANOMALY: _Anomaly.load,
}
_REQUIRED = (ArtifactKind.PRIOR, ArtifactKind.ANOMALY)


@dataclass(frozen=True, slots=True)
class PriorInputs:
    """What the sensing layer says about a payment (docs/13 §1)."""

    p_payment: Mapping[str, float]  # the payment model's classes (no MULE, docs/13 §4)
    anomaly_percentile: float  # q
    p_mule_payer: float | None  # the payer account's mule score; None without a scorer
    top_features: tuple[Mapping[str, Any], ...] = ()
    # Where the prior comes from (#291): the trained model, or a demo story's scripted prior.
    source: str = "MODEL"


class SensingInference:
    """The Guardian's ``PriorPort`` and ``p_mule(account)``. Safe to share between threads."""

    def __init__(self, engine: Engine, clock: Clock, settings: SensingConfig | None = None) -> None:
        self._engine = engine  # as delphi_app
        self._clock = clock
        self._settings = settings or load_sensing_config()
        self._mule = MuleScores(engine, clock, self._settings)
        self._models: dict[ArtifactKind, tuple[UUID, Any]] = {}
        self._lock = threading.Lock()

    def refresh(self) -> dict[ArtifactKind, UUID]:
        """Load every ACTIVE artifact not loaded yet; the ids now served."""
        with self._engine.connect() as conn:
            return self._sync(conn)

    def _sync(self, conn: Connection) -> dict[ArtifactKind, UUID]:
        active = active_artifact_ids(conn)
        for kind, loader in _LOADERS.items():
            artifact_id = active.get(kind)
            cached = self._models.get(kind)
            if artifact_id is None or (cached is not None and cached[0] == artifact_id):
                continue
            with self._lock:
                cached = self._models.get(kind)
                if cached is None or cached[0] != artifact_id:
                    self._models[kind] = (artifact_id, loader(load_blob(conn, artifact_id)))
        return active

    def prior_inputs(self, payment: bank.PaymentView) -> PriorInputs:
        """docs/13 §1 for one payment, from data before its initiation only."""
        config = self._settings.features
        with self._engine.connect() as conn:
            active = self._sync(conn)
            missing = [k.value for k in _REQUIRED if k not in active]
            if missing:
                raise DelphiError(
                    f"No ACTIVE sensing model of kind {', '.join(missing)}; train one first.",
                    code=ProblemCode.NOT_INITIALISED,
                )
            mule = ArtifactKind.MULE in active
            prior: PriorModel = self._models[ArtifactKind.PRIOR][1]
            anomaly: _Anomaly = self._models[ArtifactKind.ANOMALY][1]
            try:
                view, history = load_payment_snapshot(conn, payment.id, config)
            except LookupError as exc:
                raise NotFoundError(str(exc)) from exc
            payer, as_of = payment.payer_account_id, payment.initiated_at
            # The sweep's score if the bank had it (one query), else scored on demand below.
            p_mule_payer = cached_mule_score(conn, payer, as_of) if mule else None
        if mule and p_mule_payer is None:
            p_mule_payer = self._mule.p_mule(payer, as_of)
        features = build_payment_features(view, history, config)
        prior_row = {f: features[f] for f in prior.feature_list}
        return PriorInputs(
            p_payment=prior.predict(prior_row),
            anomaly_percentile=anomaly.percentile(features),
            p_mule_payer=p_mule_payer,
            top_features=tuple(
                {"feature": c.feature, "value": c.value, "contribution": c.contribution}
                for c in prior.contributions(prior_row)
            ),
        )

    def p_mule(self, account_id: UUID, as_of: datetime | None = None) -> float:
        """The account's mule score as of ``as_of`` (default: now), with the docs/13 §6
        fallback when there is no ACTIVE ``MULE`` model or scoring fails."""
        return self._mule.p_mule(account_id, as_of if as_of is not None else self._clock.now())
