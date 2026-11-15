"""The sensing trainers ``TRAIN_SENSING`` runs (docs/13 §4, §7, #88, #89, #159).

A trainer reads the bank as ``delphi_app`` through ``delphi.sim.api``, never ``sim_truth``: the
payments and their history, and the labels the bank had observed by the training cutoff
(``delphi.labels``, delivered by ``LABEL_DELIVERY``). ``TRAIN_SENSING`` sets the cutoff
(``TrainingRequest.cutoff``): the end of the history for the seed's training, the time it runs
for a manual retrain, so a retrain learns from the labels that arrived since (docs/13 §7).

``load_prior_data`` builds the prior model's three splits (the matured window, observed labels
only, docs/13 §4); ``train_prior_artifact`` trains on them and hands ``store_artifact`` the
blob, the report and the gate result. The tests use ``load_prior_data`` too, so they train
exactly what the job trains.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import Connection, select

from delphi.sensing.artifacts import ArtifactKind, TrainedModel
from delphi.sensing.config import SensingConfig, load_sensing_config
from delphi.sensing.features import prior_feature_names, to_us
from delphi.sensing.offline import OfflineHistories, load_bank_frames
from delphi.sensing.prior import (
    TEST,
    TRAIN,
    VALIDATION,
    LabelObservation,
    Split,
    encode_labels,
    matured_split,
    observed_labels,
    train_prior,
)
from delphi.sim import api as bank

if TYPE_CHECKING:
    from delphi.sensing.jobs import TrainingRequest

PAYMENT = "PAYMENT"


class NothingToTrainError(Exception):
    """No labelled payment falls in the matured window by the cutoff. Retrying cannot help, so
    ``TRAIN_SENSING`` records it and moves on to the next kind."""


@dataclass(frozen=True, slots=True)
class PriorData:
    """The prior model's training, validation and test splits, and what they were cut from."""

    train: Split
    validation: Split
    test: Split
    feature_list: tuple[str, ...]
    window: bank.HistoryWindow
    cutoff: datetime
    # Each split's payment ids, in row order (for tests and the near-label checks).
    ids: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]


def observed_payment_labels(
    conn: Connection, cutoff: datetime, settings: SensingConfig
) -> dict[str, str]:
    """Each payment's label as the bank knew it at ``cutoff`` (docs/05 §8)."""
    lb = bank.labels
    rows = conn.execute(
        select(lb.c.subject_id, lb.c.hypothesis_key, lb.c.label_source, lb.c.known_at).where(
            lb.c.subject_type == PAYMENT, lb.c.known_at <= cutoff
        )
    )
    return observed_labels(
        (
            LabelObservation(str(r.subject_id), r.hypothesis_key, r.label_source, to_us(r.known_at))
            for r in rows
        ),
        cutoff_us=to_us(cutoff),
        precedence=settings.prior.label_precedence,
    )


def load_prior_data(
    conn: Connection,
    window: bank.HistoryWindow,
    settings: SensingConfig | None = None,
    *,
    cutoff: datetime | None = None,
) -> PriorData:
    """The customers' labelled payments in the matured window, split by time, with their
    features (docs/13 §2–4). ``cutoff`` defaults to the end of the history."""
    settings = settings or load_sensing_config()
    cutoff = cutoff or window.end
    labels = observed_payment_labels(conn, cutoff, settings)
    p, a = bank.payments, bank.accounts
    rows = [
        r
        for r in conn.execute(
            select(p.c.id, p.c.initiated_at)
            .join(a, a.c.id == p.c.payer_account_id)
            .where(a.c.customer_id.is_not(None))
            .order_by(p.c.initiated_at, p.c.id)
        )
        if str(r.id) in labels
    ]
    ids = [str(r.id) for r in rows]
    split = matured_split(
        np.array([to_us(r.initiated_at) for r in rows], dtype=np.int64),
        history_start_us=to_us(window.start),
        cutoff_us=to_us(cutoff),
        maturity_days=window.maturity_days,
        parts=settings.prior.split_parts,
    )
    codes, known = encode_labels([labels[i] for i in ids], settings.prior.classes)
    names = prior_feature_names(settings.features)
    offline = OfflineHistories(load_bank_frames(conn), settings.features)

    def part(code: int) -> tuple[Split, tuple[str, ...]]:
        mask = (split == code) & known
        chosen = tuple(i for i, m in zip(ids, mask, strict=True) if m)
        matrix = offline.features(chosen)[list(names)].to_numpy(dtype=np.float64)
        return Split(matrix.reshape(len(chosen), len(names)), codes[mask]), chosen

    (train, train_ids), (validation, val_ids), (test, test_ids) = (
        part(TRAIN),
        part(VALIDATION),
        part(TEST),
    )
    return PriorData(train, validation, test, names, window, cutoff, (train_ids, val_ids, test_ids))


def train_prior_artifact(request: TrainingRequest) -> TrainedModel:
    """``TRAIN_SENSING``'s PRIOR trainer (docs/13 §4)."""
    settings = load_sensing_config()
    window = bank.history_window(request.dataset)
    data = load_prior_data(request.conn, window, settings, cutoff=request.cutoff)
    if len(data.train) == 0:
        raise NothingToTrainError(
            f"no labelled payment in the matured window by {request.cutoff.isoformat()}"
        )
    model, gate = train_prior(
        data.train,
        data.validation,
        data.test,
        data.feature_list,
        settings.prior,
        profile=request.dataset.profile,
    )
    return TrainedModel(
        kind=ArtifactKind.PRIOR,
        blob=model.to_bytes(),
        metrics=dict(model.report),
        feature_list=data.feature_list,
        training_cutoff=request.cutoff,
        gate_passed=gate.passed,
        gate_reasons=gate.reasons,
    )
