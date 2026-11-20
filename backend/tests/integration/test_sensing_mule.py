"""The mule scorer's training data on a generated dataset (docs/13 §6, FR-SNS-05, #91).

The `test` dataset is written and its labels delivered as the seed does, then the accounts are
loaded exactly as `TRAIN_SENSING`'s MULE trainer loads them, as `delphi_app`, from the bank
and the observed labels only.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sensing.config import load_sensing_config
from delphi.sensing.features import ACCOUNT_FEATURES, to_us
from delphi.sensing.mule import train_mule
from delphi.sensing.trainers import MuleData, load_mule_data, mule_label_times
from delphi.sim.api import DatasetInfo, history_window
from delphi.sim.dataset import generate_dataset
from delphi.sim.delivery import deliver_labels
from delphi.sim.generator_config import load_generator_config
from delphi.sim.writer import write_dataset

from ._db import engine_as, upgrade

DAY_US = 86_400_000_000
COMPLAINTS = ACCOUNT_FEATURES.index("complaints_linked")


def _load(
    create_database: Callable[[], str], kb: KnowledgeBase, profile: str
) -> tuple[MuleData, dict[str, int]]:
    url = create_database()
    upgrade(url)
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
            data = load_mule_data(conn, window)
            labelled = mule_label_times(conn, window.end, load_sensing_config())
    finally:
        app.dispose()
    return data, labelled


@pytest.fixture(scope="module")
def loaded(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> tuple[MuleData, dict[str, int]]:
    return _load(create_database, kb, "test")


def _rows(data: MuleData) -> list[tuple[str, int, int, list[float]]]:
    out = []
    for split, rows in zip((data.train, data.validation, data.test), data.rows, strict=True):
        for (account, as_of), label, features in zip(
            rows, split.labels, split.features, strict=True
        ):
            out.append((account, as_of, int(label), list(features)))
    return out


def test_the_test_profile_has_labelled_mules_and_non_mules(
    loaded: tuple[MuleData, dict[str, int]],
) -> None:
    data, labelled = loaded
    assert labelled, "the test dataset's registry listings and complaints label some accounts"
    assert data.feature_list == ACCOUNT_FEATURES
    assert 0 < int(data.train.labels.sum()) < len(data.train)


def test_a_mules_features_are_taken_before_its_first_label_event(
    loaded: tuple[MuleData, dict[str, int]],
) -> None:
    """#142 review: no training mule may already show the complaint or listing that labels it."""
    data, labelled = loaded
    mules = [(a, as_of, f) for a, as_of, label, f in _rows(data) if label == 1]
    assert mules
    for account, as_of, features in mules:
        assert as_of == labelled[account] - 1
        assert features[COMPLAINTS] == 0.0


def test_non_mules_are_scored_at_the_end_of_the_matured_window(
    loaded: tuple[MuleData, dict[str, int]],
) -> None:
    data, labelled = loaded
    end = to_us(data.cutoff) - data.window.maturity_days * DAY_US
    min_age_days = load_sensing_config().mule.negative_min_age_days
    clean = [(a, as_of, f) for a, as_of, label, f in _rows(data) if label == 0]
    assert clean
    for account, as_of, features in clean:
        assert account not in labelled
        assert as_of == end
        assert features[ACCOUNT_FEATURES.index("account_age_days")] >= min_age_days


def test_the_loaded_splits_train_a_reported_model(
    loaded: tuple[MuleData, dict[str, int]],
) -> None:
    data, _ = loaded
    model, gate = train_mule(
        data.train,
        data.validation,
        data.test,
        data.feature_list,
        load_sensing_config().mule,
        profile="test",
    )
    assert not gate.enforced and gate.passed  # `test` reports its gate only
    assert model.report["train"]["mules"] == int(data.train.labels.sum())


@pytest.mark.skipif(
    os.environ.get("DELPHI_RUN_SLOW") != "1", reason="slow: set DELPHI_RUN_SLOW=1 to run"
)
def test_the_eval_profile_passes_the_gate(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    """docs/13 §6, docs/25 M3: mule AUC ≥ 0.90 on eval's test split, or a documented deviation.

    Run with ``DELPHI_RUN_SLOW=1 uv run pytest -s`` to see the printed training report.
    """
    data, _ = _load(create_database, kb, "eval")
    model, gate = train_mule(
        data.train,
        data.validation,
        data.test,
        data.feature_list,
        load_sensing_config().mule,
        profile="eval",
    )
    print(model.report)
    assert gate.enforced
    assert gate.passed, gate.reasons
