"""The prior model trained on a generated dataset (docs/13 §4, FR-SNS-03).

The dataset is written and its history's labels delivered into `delphi.labels` as the seed does
(#159), then the training data is loaded exactly as `TRAIN_SENSING`'s PRIOR trainer loads it
(`delphi.sensing.trainers.load_prior_data`), from the bank and the observed labels only.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sensing.config import load_sensing_config
from delphi.sensing.prior import GateResult, PriorModel, Split, train_prior
from delphi.sensing.trainers import PriorData, load_prior_data
from delphi.sim.api import DatasetInfo, history_window
from delphi.sim.dataset import generate_dataset
from delphi.sim.delivery import deliver_labels
from delphi.sim.generator_config import load_generator_config
from delphi.sim.writer import write_dataset

from ._db import engine_as, upgrade


@dataclass(frozen=True)
class Trained:
    model: PriorModel
    gate: GateResult
    test: Split
    data: PriorData


def _train(create_database: Callable[[], str], kb: KnowledgeBase, profile: str) -> Trained:
    settings = load_sensing_config()
    url = create_database()
    upgrade(url)
    # `test` and `eval` end their history at the generator's fixed epoch, whatever created_at is.
    dataset = DatasetInfo(uuid4(), profile, 1, datetime(2026, 11, 1, tzinfo=UTC))
    window = history_window(dataset)
    sim = engine_as(url, "delphi_sim")
    with sim.begin() as conn:
        write_dataset(conn, generate_dataset(load_generator_config(), profile, kb=kb))
        deliver_labels(
            conn, until=window.end, active_hypotheses=frozenset(kb.hypotheses), now=window.end
        )
    sim.dispose()
    app = engine_as(url, "delphi_app")
    try:
        with app.connect() as conn:
            data = load_prior_data(conn, window, settings)
    finally:
        app.dispose()
    model, result = train_prior(
        data.train, data.validation, data.test, data.feature_list, settings.prior, profile=profile
    )
    return Trained(model, result, data.test, data)


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
