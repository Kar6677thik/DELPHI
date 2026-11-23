"""One evaluation run: every policy over the same stream, in parallel (docs/22 §1, #123 PR 4).

Each policy replays the whole stream on its own copy of the bank, so policies are independent
and run in worker processes (``workers``, docs/22 §1: 4). The work is split by policy, not by
customer: a policy's bank state is shared across its customers (payees receive from many
customers, balances move), so splitting one policy's stream would change what it sees. Every
random quantity comes from per-payment streams (#123 plan §3), so the outcomes are the same for
any number of workers; ``test_one_worker_and_four_give_the_same_outcomes`` checks it.

The dataset and the trained sensing are built once in the parent and shared with the workers by
``fork`` (copy-on-write), so the 4.5 GiB budget of docs/22 §1 holds.
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

WORKERS = 4  # docs/22 §1

# Set in the parent before the pool forks; each worker reads its copy.
_SHARED: dict[str, object] = {}


@dataclass(frozen=True, slots=True)
class RunResult:
    outcomes: dict[str, tuple[PaymentOutcome, ...]]  # policy name → its outcomes


def _replay_one(index: int) -> tuple[str, tuple[PaymentOutcome, ...]]:
    dataset = _SHARED["dataset"]
    policies = _SHARED["policies"]
    assert isinstance(dataset, Dataset) and isinstance(policies, list)
    policy: Policy = policies[index]
    models = _SHARED["models"]
    seed = _SHARED["seed"]
    assert isinstance(models, ResponseModels) and isinstance(seed, int)
    return policy.name, replay(dataset, policy, seed=seed, models=models)


def run_policies(
    dataset: Dataset,
    policies: Sequence[Policy],
    *,
    seed: int,
    models: ResponseModels,
    workers: int = WORKERS,
) -> RunResult:
    """Replay every policy; ``workers`` > 1 runs them in forked processes."""
    names = [p.name for p in policies]
    if len(set(names)) != len(names):
        raise ValueError("policy names must be unique")
    _SHARED.update(dataset=dataset, policies=list(policies), models=models, seed=seed)
    try:
        if workers <= 1:
            pairs = [_replay_one(i) for i in range(len(policies))]
        else:
            context = multiprocessing.get_context("fork")
            with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
                pairs = list(pool.map(_replay_one, range(len(policies))))
    finally:
        _SHARED.clear()
    return RunResult(dict(pairs))
