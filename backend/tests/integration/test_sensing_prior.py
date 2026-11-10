"""The prior model trained on a generated dataset (docs/13 §4, FR-SNS-03).

The bank has no table of observed labels yet (docs/17 `labels`, delivered by LABEL_DELIVERY), so
this test builds "what the bank had observed by the cutoff" from the label schedule: the labels
due by then. Only the test reads `sim_truth`; the trainer sees features and observed labels.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pytest
from sqlalchemy import select

from delphi.kb.model import KnowledgeBase
from delphi.sensing.config import SensingConfig, load_sensing_config
from delphi.sensing.features import prior_feature_names, to_us
from delphi.sensing.offline import OfflineHistories, load_bank_frames
from delphi.sensing.prior import (
    TEST,
    TRAIN,
    VALIDATION,
    GateResult,
    LabelObservation,
    PriorModel,
    Split,
    encode_labels,
    matured_split,
    observed_labels,
    train_prior,
)
from delphi.sim import api as bank
from delphi.sim import truth
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import history_end, load_generator_config
from delphi.sim.writer import write_dataset

from ._db import engine_as, upgrade


@dataclass(frozen=True)
class Trained:
    model: PriorModel
    gate: GateResult
    test: Split


def _train(create_database: Callable[[], str], kb: KnowledgeBase, profile: str) -> Trained:
    generator = load_generator_config()
    settings: SensingConfig = load_sensing_config()
    url = create_database()
    upgrade(url)
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:
        write_dataset(conn, generate_dataset(generator, profile, kb=kb))
    end = history_end(generator, profile)
    cutoff_us = to_us(end)
    params = generator.profiles[profile]
    with sim.connect() as conn:  # the test's stand-in for the bank's labels table
        ls = truth.label_schedule
        observations = [
            LabelObservation(str(r.subject_id), r.hypothesis_key, r.label_source, to_us(r.due_at))
            for r in conn.execute(
                select(ls.c.subject_id, ls.c.hypothesis_key, ls.c.label_source, ls.c.due_at).where(
                    ls.c.subject_type == "PAYMENT"
                )
            )
        ]
    sim.dispose()
    labels = observed_labels(
        observations, cutoff_us=cutoff_us, precedence=settings.prior.label_precedence
    )

    app = engine_as(url, "delphi_app")
    try:
        with app.connect() as conn:
            p, a = bank.payments, bank.accounts
            rows = conn.execute(
                select(p.c.id, p.c.initiated_at)
                .join(a, a.c.id == p.c.payer_account_id)
                .where(a.c.customer_id.is_not(None))
                .order_by(p.c.initiated_at, p.c.id)
            ).all()
            frames = load_bank_frames(conn)
    finally:
        app.dispose()
    ids = [str(r.id) for r in rows if str(r.id) in labels]
    at = np.array([to_us(r.initiated_at) for r in rows if str(r.id) in labels], dtype=np.int64)
    split = matured_split(
        at,
        history_start_us=to_us(end - timedelta(days=params.history_days)),
        cutoff_us=cutoff_us,
        maturity_days=params.maturity_days,
        parts=settings.prior.split_parts,
    )
    codes, known = encode_labels([labels[i] for i in ids], settings.prior.classes)
    names = list(prior_feature_names(settings.features))
    offline = OfflineHistories(frames, settings.features)

    def part(code: int) -> Split:
        mask = (split == code) & known
        chosen = [i for i, m in zip(ids, mask, strict=True) if m]
        matrix = offline.features(chosen)[names].to_numpy(dtype=np.float64)
        return Split(matrix.reshape(len(chosen), len(names)), codes[mask])

    model, result = train_prior(
        part(TRAIN), part(VALIDATION), part(TEST), names, settings.prior, profile=profile
    )
    return Trained(model, result, part(TEST))


def test_the_test_profile_trains_and_reports(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    trained = _train(create_database, kb, "test")
    report = trained.model.report
    print(report)
    assert report["n_train"] > 0 and report["n_validation"] > 0 and report["n_test"] > 0
    assert report["temperature"] > 0
    assert not trained.gate.enforced and trained.gate.passed  # `test` is reported only
    probabilities = trained.model.probabilities(trained.test.features)
    assert np.allclose(probabilities.sum(axis=1), 1.0)
    # The blob round-trips to the same probabilities and contributions.
    again = PriorModel.from_bytes(trained.model.to_bytes())
    assert np.array_equal(again.probabilities(trained.test.features), probabilities)
    row = dict(zip(trained.model.feature_list, trained.test.features[0], strict=True))
    assert again.contributions(row) == trained.model.contributions(row)
    assert len(trained.model.contributions(row)) == load_sensing_config().prior.top_contributions


@pytest.mark.skipif(
    os.environ.get("DELPHI_RUN_SLOW") != "1", reason="slow: set DELPHI_RUN_SLOW=1 to run"
)
def test_the_eval_profile_passes_the_gate(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    """FR-SNS-03 acceptance: on `eval`, ECE ≤ 0.05 and macro AUC ≥ 0.85."""
    trained = _train(create_database, kb, "eval")
    print(trained.model.report)
    assert trained.gate.enforced
    assert trained.gate.passed, trained.gate.reasons
