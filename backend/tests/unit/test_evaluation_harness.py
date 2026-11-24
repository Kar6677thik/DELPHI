"""One evaluation run over every policy (docs/22 §1, #123 PR 4): the outcomes do not depend on
the number of workers, and the `eval` runtime stays within the docs/22 §1 budget (slow)."""

from __future__ import annotations

import multiprocessing
import os
import time

import pytest

from delphi.engine import compile_profile
from delphi.evaluation.delphi_policy import Delphi
from delphi.evaluation.harness import run_policies, start_method
from delphi.evaluation.replay import B1, B2, Policy
from delphi.evaluation.sensing import train_and_score
from delphi.kb.model import KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.response_models import load_response_models

SEED = 7
RUNTIME_BUDGET_S = 30 * 60  # docs/22 §1: ≤ 30 minutes for `eval`


def _policies(dataset: Dataset, kb: KnowledgeBase, config: GeneratorConfig) -> list[Policy]:
    sensing = train_and_score(dataset, kb, config)
    profile = compile_profile(kb, "GUARDIAN_PAYMENT")
    kw = {"sensing": sensing.inputs}
    return [
        B1(config, kb.config.classifiers.new_device_days),
        B2(config, profile, sensing.inputs),
        Delphi(kb, profile, **kw),
        Delphi.myopic(kb, profile, **kw),
        Delphi.no_active(kb, profile, **kw),
    ]


def test_one_worker_and_four_give_the_same_outcomes(kb: KnowledgeBase) -> None:
    config = load_generator_config()
    dataset = generate_dataset(config, "test", kb=kb)
    models = load_response_models()
    one = run_policies(dataset, _policies(dataset, kb, config), seed=SEED, models=models, workers=1)
    four = run_policies(
        dataset, _policies(dataset, kb, config), seed=SEED, models=models, workers=4
    )
    assert one == four
    assert set(one.outcomes) == {"B1", "B2", "DELPHI", "DELPHI-myopic", "DELPHI-no-active"}


def test_the_start_method_falls_back_to_spawn_without_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no ``fork``; the workers then start with ``spawn``."""
    assert start_method() in ("fork", "spawn")
    monkeypatch.setattr(multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    assert start_method() == "spawn"


@pytest.mark.slow
def test_eval_runs_within_the_budget(kb: KnowledgeBase) -> None:
    """docs/22 §1: generate `eval`, train sensing, replay every policy on 4 workers, ≤ 30 min."""
    if os.environ.get("DELPHI_RUN_SLOW") != "1":
        pytest.skip("set DELPHI_RUN_SLOW=1 to run the `eval` runtime check")
    started = time.monotonic()
    config = load_generator_config()
    dataset = generate_dataset(config, "eval", kb=kb)
    result = run_policies(
        dataset, _policies(dataset, kb, config), seed=SEED, models=load_response_models()
    )
    elapsed = time.monotonic() - started
    print(f"eval run: {elapsed / 60:.1f} min, {len(result.outcomes['B1'])} stream payments")
    assert elapsed <= RUNTIME_BUDGET_S
