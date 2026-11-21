"""The sensing trainers ``TRAIN_SENSING`` runs (docs/13 §4, §7, #88, #89, #159).

A trainer reads the bank as ``delphi_app`` through ``delphi.sim.api``, never ``sim_truth``: the
payments and their history, and the labels the bank had observed by the training cutoff
(``delphi.labels``, delivered by ``LABEL_DELIVERY``). ``TRAIN_SENSING`` sets the cutoff
(``TrainingRequest.cutoff``): the end of the history for the seed's training, the time it runs
for a manual retrain, so a retrain learns from the labels that arrived since (docs/13 §7).

``load_prior_data`` builds the prior model's three splits (the matured window, observed labels
only, docs/13 §4); ``train_prior_artifact`` trains on them and hands ``store_artifact`` the
blob, the report and the gate result. ``train_anomaly_artifact`` fits the anomaly detector on
the GENUINE rows of the same training split, with the same feature list (docs/13 §5).
``load_mule_data`` and ``train_mule_artifact`` do the same for the mule scorer over accounts
(docs/13 §6). The tests use the loaders too, so they train exactly what the job trains.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import Connection, select

from delphi.sensing.anomaly import train_anomaly_matrix
from delphi.sensing.artifacts import ArtifactKind, TrainedModel
from delphi.sensing.config import SensingConfig, load_sensing_config
from delphi.sensing.features import (
    ACCOUNT_FEATURES,
    PROBE_INPUT_FEATURES,
    AccountHistory,
    build_account_features,
    prior_feature_names,
    to_us,
)
from delphi.sensing.mule import MuleSplit, train_mule
from delphi.sensing.offline import OfflineAccountHistories, OfflineHistories, load_bank_frames
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


GENUINE = "GENUINE"


def train_anomaly_artifact(request: TrainingRequest) -> TrainedModel:
    """``TRAIN_SENSING``'s ANOMALY trainer (docs/13 §5): the isolation forest on the prior
    model's training split, GENUINE rows only, with the prior's feature list. ``q`` feeds the
    prior's open mass, so the double-count guard of docs/13 §3 applies: no probe inputs.

    The anomaly detector has no quality gate (docs/13 §7 gates the prior and the mule scorer),
    so its artifact is always activated."""
    settings = load_sensing_config()
    window = bank.history_window(request.dataset)
    data = load_prior_data(request.conn, window, settings, cutoff=request.cutoff)
    if set(data.feature_list) & PROBE_INPUT_FEATURES:  # docs/13 §3, §5
        raise ValueError("the anomaly detector must not see probe inputs")
    genuine = data.train.features[data.train.labels == settings.prior.classes.index(GENUINE)]
    if len(genuine) == 0:
        raise NothingToTrainError(
            f"no GENUINE-labelled payment in the training split by {request.cutoff.isoformat()}"
        )
    model = train_anomaly_matrix(genuine, data.feature_list, settings.anomaly)
    return TrainedModel(
        kind=ArtifactKind.ANOMALY,
        blob=model.to_bytes(),
        metrics=model.metrics(),
        feature_list=data.feature_list,
        training_cutoff=request.cutoff,
        gate_passed=True,
    )


ACCOUNT = "ACCOUNT"
_DAY_US = 86_400_000_000


@dataclass(frozen=True, slots=True)
class MuleData:
    """The mule scorer's splits over accounts, and what they were cut from (docs/13 §6)."""

    train: MuleSplit
    validation: MuleSplit
    test: MuleSplit
    feature_list: tuple[str, ...]
    window: bank.HistoryWindow
    cutoff: datetime
    # Each split's (account id, as_of in µs), in row order.
    rows: tuple[tuple[tuple[str, int], ...], ...]


def mule_label_times(conn: Connection, cutoff: datetime, settings: SensingConfig) -> dict[str, int]:
    """Each account's first MULE label event the bank knew by ``cutoff`` (µs): an ACCOUNT label
    with the MULE key (registry listing, investigator disposition), or a complaint naming it as
    a counterparty (docs/13 §6)."""
    lb, c = bank.labels, bank.complaints
    first: dict[str, int] = {}

    def note(account: object, at: datetime) -> None:
        key, us = str(account), to_us(at)
        if key not in first or us < first[key]:
            first[key] = us

    for r in conn.execute(
        select(lb.c.subject_id, lb.c.known_at).where(
            lb.c.subject_type == ACCOUNT,
            lb.c.hypothesis_key == settings.mule.label_hypothesis,
            lb.c.known_at <= cutoff,
        )
    ):
        note(r.subject_id, r.known_at)
    for r in conn.execute(
        select(c.c.counterparty_account_ids, c.c.reported_at).where(c.c.reported_at <= cutoff)
    ):
        for account in r.counterparty_account_ids:
            note(account, r.reported_at)
    return first


def load_mule_data(
    conn: Connection,
    window: bank.HistoryWindow,
    settings: SensingConfig | None = None,
    *,
    cutoff: datetime | None = None,
) -> MuleData:
    """The labelled accounts, each with its own ``as_of``, split by first activity (docs/13 §6).

    - **Mules:** accounts with a MULE label event by the cutoff. Their features are taken just
      before the first such event, so the model never learns "a complaint means mule" (#142
      review).
    - **Non-mules:** accounts with no such event, opened at least ``negative_min_age_days``
      before their ``as_of``, which is the end of the matured window.
    - Both need an executed payment before their ``as_of``. The split is by the date of the
      first one, with the prior's 2/3, 1/6, 1/6 parts over the matured window (§4).
    """
    settings = settings or load_sensing_config()
    cutoff = cutoff or window.end
    cutoff_us = to_us(cutoff)
    matured_end_us = cutoff_us - window.maturity_days * _DAY_US
    min_age_us = int(settings.mule.negative_min_age_days * _DAY_US)
    labelled = mule_label_times(conn, cutoff, settings)
    frames = load_bank_frames(conn)
    offline = OfflineAccountHistories(frames, settings.accounts)

    # account, as_of, label, first activity, and the snapshot (built once per account)
    candidates: list[tuple[str, int, int, int, AccountHistory]] = []
    for row in frames.accounts.itertuples(index=False):
        account = str(row.id)
        history = offline.snapshot(account)
        if account in labelled:
            as_of, label = labelled[account] - 1, 1
        elif history.opened_at_us <= matured_end_us - min_age_us:
            as_of, label = matured_end_us, 0
        else:
            continue
        flows = np.concatenate([history.inbound_at_us, history.outbound_at_us])
        flows = flows[flows < as_of]
        if flows.size == 0:
            continue
        candidates.append((account, as_of, label, int(flows.min()), history))
    candidates.sort(key=lambda c: (c[3], c[0]))
    split = matured_split(
        np.array([c[3] for c in candidates], dtype=np.int64),
        history_start_us=to_us(window.start),
        cutoff_us=cutoff_us,
        maturity_days=window.maturity_days,
        parts=settings.prior.split_parts,
    )

    def part(code: int) -> tuple[MuleSplit, tuple[tuple[str, int], ...]]:
        chosen = [c for c, s in zip(candidates, split, strict=True) if s == code]
        matrix = np.array(
            [
                [
                    build_account_features(history, as_of, settings.accounts)[f]
                    for f in ACCOUNT_FEATURES
                ]
                for _, as_of, _, _, history in chosen
            ],
            dtype=np.float64,
        ).reshape(len(chosen), len(ACCOUNT_FEATURES))
        labels = np.array([c[2] for c in chosen], dtype=np.int64)
        return MuleSplit(matrix, labels), tuple((a, as_of) for a, as_of, *_ in chosen)

    (train, r1), (validation, r2), (test, r3) = part(TRAIN), part(VALIDATION), part(TEST)
    return MuleData(train, validation, test, ACCOUNT_FEATURES, window, cutoff, (r1, r2, r3))


def train_mule_artifact(request: TrainingRequest) -> TrainedModel:
    """``TRAIN_SENSING``'s MULE trainer (docs/13 §6)."""
    settings = load_sensing_config()
    window = bank.history_window(request.dataset)
    data = load_mule_data(request.conn, window, settings, cutoff=request.cutoff)
    mules = int(data.train.labels.sum())
    if not 0 < mules < len(data.train):
        raise NothingToTrainError(
            "the mule scorer needs mules and non-mules in the training split by "
            f"{request.cutoff.isoformat()} ({mules} of {len(data.train)} accounts are mules)"
        )
    model, gate = train_mule(
        data.train,
        data.validation,
        data.test,
        data.feature_list,
        settings.mule,
        profile=request.dataset.profile,
    )
    return TrainedModel(
        kind=ArtifactKind.MULE,
        blob=model.to_bytes(),
        metrics=dict(model.report),
        feature_list=data.feature_list,
        training_cutoff=request.cutoff,
        gate_passed=gate.passed,
        gate_reasons=gate.reasons,
    )
