"""Sensing for the evaluation harness (docs/22 §1 step 2, #123 plan §1).

The prior and the anomaly detector are trained, in memory, on the history labels the bank had
observed when the history ended (day 150 on `eval`). This is the same path as ``TRAIN_SENSING``:
the shared ``prior_data`` core over the same bank frames, built here from the dataset's rows
instead of the database. Then every stream payment is scored as of its initiation, with the
window's earlier payments in its history, as the live sensing would score it. The scores depend
on nothing a policy decides, so they are computed once and shared by every policy.

Labels: the scheduled labels due by the end of the history, mapped as the bank records them at
delivery (``observed_hypothesis`` under KB v1, docs/14 §7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

import numpy as np

from delphi.evaluation.stream import eval_stream
from delphi.guardian.api import PriorInputs
from delphi.kb.model import KnowledgeBase
from delphi.sensing.anomaly import train_anomaly_matrix
from delphi.sensing.config import SensingConfig, load_sensing_config
from delphi.sensing.features import prior_feature_names, to_us
from delphi.sensing.offline import OfflineHistories, frames_from_records
from delphi.sensing.prior import LabelObservation, feature_matrix, observed_labels, train_prior
from delphi.sensing.trainers import prior_data
from delphi.sim.api import HistoryWindow
from delphi.sim.dataset import Dataset
from delphi.sim.export import dataset_records
from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.labels import observed_hypothesis

PAYMENT = "PAYMENT"
GENUINE = "GENUINE"


@dataclass(frozen=True, slots=True)
class HarnessSensing:
    """Each stream payment's sensing outputs (the Guardian's ``PriorInputs``), and the trained
    models' reports."""

    inputs: Mapping[UUID, PriorInputs]
    prior_report: Mapping[str, object]


def train_and_score(
    dataset: Dataset,
    kb: KnowledgeBase,
    config: GeneratorConfig,
    settings: SensingConfig | None = None,
) -> HarnessSensing:
    settings = settings or load_sensing_config()
    pop, history = dataset.population, dataset.history
    end = pop.history_end
    window = HistoryWindow(pop.history_start, end, config.profiles[pop.profile].maturity_days)
    active = frozenset(kb.hypotheses)
    labels = observed_labels(
        (
            LabelObservation(
                str(label.subject_id),
                observed_hypothesis(label, active),
                label.label_source.value,
                to_us(label.due_at),
            )
            for label in history.labels
            if label.subject_type.value == PAYMENT and label.due_at <= end
        ),
        cutoff_us=to_us(end),
        precedence=settings.prior.label_precedence,
    )
    customers = {a.id for a in pop.accounts if a.customer_id is not None}
    payments = [
        (str(p.id), p.initiated_at)
        for p in history.payments
        if p.payer_account_id in customers and p.initiated_at < end
    ]
    frames = frames_from_records(dataset_records(dataset))
    data = prior_data(labels, payments, frames, window, settings, cutoff=end)
    model, _gate = train_prior(
        data.train,
        data.validation,
        data.test,
        data.feature_list,
        settings.prior,
        profile=pop.profile,
    )
    genuine = data.train.features[data.train.labels == settings.prior.classes.index(GENUINE)]
    anomaly = train_anomaly_matrix(genuine, data.feature_list, settings.anomaly)
    stream = [str(i.payment.id) for i in eval_stream(dataset)]
    names = list(prior_feature_names(settings.features))
    features = OfflineHistories(frames, settings.features).features(tuple(stream))
    # One matrix, scored in one call per model: row by row, the forest alone took ~26 ms a
    # payment, most of train_and_score's 106 min on `eval`'s 430k stream payments (#261). Each
    # row's result is the same.
    rows = [
        {str(k): float(v) for k, v in raw.items()} for raw in features[names].to_dict("records")
    ]
    p_payment = model.probabilities(feature_matrix(rows, model.feature_list))
    scores = anomaly.scores(feature_matrix(rows, anomaly.feature_list))
    q = np.searchsorted(anomaly.quantiles, scores, side="right") / len(anomaly.quantiles)
    inputs: dict[UUID, PriorInputs] = {}
    for n, payment_id in enumerate(stream):
        inputs[UUID(payment_id)] = PriorInputs(
            p_payment=dict(zip(model.classes, map(float, p_payment[n]), strict=True)),
            anomaly_percentile=float(q[n]),
            p_mule_payer=None,  # no mule scorer in the harness: MULE is KB-only (docs/06 §11)
        )
    return HarnessSensing(inputs, dict(model.report))
