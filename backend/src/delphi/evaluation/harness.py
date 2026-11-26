"""One evaluation run: every policy over the same stream, in parallel (docs/22 §1, #123 PR 4).

Each policy replays the whole stream on its own copy of the bank, so policies are independent
and run in worker processes (``workers``, docs/22 §1: 4). The work is split by policy, not by
customer: a policy's bank state is shared across its customers (payees receive from many
customers, balances move), so splitting one policy's stream would change what it sees. Every
random quantity comes from per-payment streams (#123 plan §3), so the outcomes are the same for
any number of workers; ``test_one_worker_and_four_give_the_same_outcomes`` checks it.

The dataset and the trained sensing are built once in the parent and handed to each worker by
the pool's initializer. With ``fork`` (Linux, macOS) the workers inherit them without a copy at
start; CPython's reference counts still touch the pages a worker reads, so walking the whole
history copies part of them (the measured RSS is on #123). Where ``fork`` does not exist
(Windows), ``spawn`` pickles them to each worker instead: correct, but slower and larger.

The entry point (a CLI command and the evaluation job writing the report) comes with #124.
"""

from __future__ import annotations

import multiprocessing
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from delphi.evaluation.outcomes import PaymentOutcome
from delphi.evaluation.replay import Policy, replay
from delphi.sim.dataset import Dataset
from delphi.sim.response_models import ResponseModels

WORKERS = 4  # docs/22 §1; a deployment sets DELPHI_EVAL_WORKERS (``Settings.eval_workers``)

# Set in each worker by ``_share`` (and in the parent when running in-process).
_SHARED: dict[str, object] = {}


@dataclass(frozen=True, slots=True)
class RunResult:
    outcomes: dict[str, tuple[PaymentOutcome, ...]]  # policy name → its outcomes


def _share(dataset: Dataset, policies: list[Policy], models: ResponseModels, seed: int) -> None:
    _SHARED.update(dataset=dataset, policies=policies, models=models, seed=seed)


def _replay_one(index: int) -> tuple[str, tuple[PaymentOutcome, ...]]:
    dataset = _SHARED["dataset"]
    policies = _SHARED["policies"]
    assert isinstance(dataset, Dataset) and isinstance(policies, list)
    policy: Policy = policies[index]
    models = _SHARED["models"]
    seed = _SHARED["seed"]
    assert isinstance(models, ResponseModels) and isinstance(seed, int)
    return policy.name, replay(dataset, policy, seed=seed, models=models)


def start_method() -> str:
    """``fork`` where the platform has it, else ``spawn`` (Windows)."""
    return "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"


def run_policies(
    dataset: Dataset,
    policies: Sequence[Policy],
    *,
    seed: int,
    models: ResponseModels,
    workers: int = WORKERS,
) -> RunResult:
    """Replay every policy; ``workers`` > 1 runs them in worker processes."""
    names = [p.name for p in policies]
    if len(set(names)) != len(names):
        raise ValueError("policy names must be unique")
    shared = (dataset, list(policies), models, seed)
    try:
        if workers <= 1:
            _share(*shared)
            pairs = [_replay_one(i) for i in range(len(policies))]
        else:
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context(start_method()),
                initializer=_share,
                initargs=shared,
            ) as pool:
                pairs = list(pool.map(_replay_one, range(len(policies))))
    finally:
        _SHARED.clear()
    return RunResult(dict(pairs))
