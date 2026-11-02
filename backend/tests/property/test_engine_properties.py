"""Engine invariants 1–10 (docs/21 §2)."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from delphi.engine import (
    Budgets,
    CompiledProfile,
    Phase,
    SelectedBy,
    bayes_risk,
    build_prior,
    initial_budgets,
    loss_matrix,
    open_mass,
    plan_next,
    update,
    voi_myopic,
)
from delphi.engine.planner import _ValueFunction
from tests.property.strategies import belief_for, profile

PROPS = settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
SLOW_PROPS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
amounts = st.floats(0.0, 1e8)


@PROPS
@given(st.data())
def test_1_posterior_is_a_distribution(data: st.DataObject) -> None:
    prof: CompiledProfile = data.draw(profile())
    p = data.draw(belief_for(len(prof.hypotheses)))
    src = data.draw(st.sampled_from(list(prof.sources.values())))
    o = data.draw(st.integers(0, len(src.outcomes) - 1))
    post = update(p, src.likelihood[o])
    assert np.all(post >= 0)
    assert post.sum() == pytest.approx(1.0, abs=1e-12)


@PROPS
@given(st.data())
def test_2_observation_order_does_not_matter(data: st.DataObject) -> None:
    prof: CompiledProfile = data.draw(profile())
    p = data.draw(belief_for(len(prof.hypotheses)))
    obs = [(s, data.draw(st.integers(0, len(s.outcomes) - 1))) for s in prof.sources.values()]
    forward = p
    for s, o in obs:
        forward = update(forward, s.likelihood[o])
    backward = p
    for s, o in reversed(obs):
        backward = update(backward, s.likelihood[o])
    np.testing.assert_allclose(forward, backward, atol=1e-9)


@PROPS
@given(st.data())
def test_3_uninformative_source_leaves_belief_unchanged(data: st.DataObject) -> None:
    prof: CompiledProfile = data.draw(profile())
    p = data.draw(belief_for(len(prof.hypotheses)))
    n_o = data.draw(st.integers(2, 4))
    col = np.random.default_rng(data.draw(st.integers(0, 2**32 - 1))).dirichlet(np.ones(n_o))
    lik = np.tile(col[:, None], (1, len(p)))
    for o in range(n_o):
        np.testing.assert_allclose(update(p, lik[o]), p, atol=1e-12)


@PROPS
@given(st.data(), amounts, st.booleans())
def test_4_myopic_voi_is_non_negative(data: st.DataObject, amount: float, vuln: bool) -> None:
    prof: CompiledProfile = data.draw(profile())
    p = data.draw(belief_for(len(prof.hypotheses)))
    losses = loss_matrix(prof, amount, vuln)
    scale = max(1.0, float(np.abs(losses).max()))
    for src in prof.sources.values():
        assert voi_myopic(p, src.likelihood, losses) >= -1e-9 * scale


@SLOW_PROPS
@given(st.data(), amounts)
def test_5_lookahead_value_bounded_and_monotone(data: st.DataObject, amount: float) -> None:
    prof: CompiledProfile = data.draw(profile(max_sources=4))
    p = data.draw(belief_for(len(prof.hypotheses)))
    losses = loss_matrix(prof, amount, False)
    vf = _ValueFunction(prof, losses)
    keys = frozenset(prof.sources)
    budgets = initial_budgets(prof)
    values = [vf.value(p, keys, budgets, k) for k in range(4)]
    tol = 1e-6 * max(1.0, values[0])
    assert values[0] == pytest.approx(bayes_risk(p, losses))
    for a, b in pairwise(values):
        assert b <= a + tol  # V_k non-increasing in k, and V_k ≤ R(p)


@SLOW_PROPS
@given(st.data(), amounts)
def test_6_stop_iff_no_net_value_above_epsilon(data: st.DataObject, amount: float) -> None:
    prof: CompiledProfile = data.draw(profile(all_active=True))
    p = data.draw(belief_for(len(prof.hypotheses)))
    plan = plan_next(
        prof,
        p,
        amount_paise=amount,
        vulnerable=False,
        phase=Phase.GUARDIAN_ACTIVE,
        budgets=initial_budgets(prof),
    )
    nets = [c.net_value_paise for c in plan.candidates if c.net_value_paise is not None]
    if plan.is_stop:
        assert all(v <= prof.stop_epsilon_paise for v in nets)
    else:
        assert max(nets) > prof.stop_epsilon_paise
        chosen = next(c for c in plan.candidates if c.source_key == plan.action)
        assert chosen.net_value_paise is not None
        assert chosen.net_value_paise >= max(nets) - 1.0  # within the 1-paise tie tolerance


@SLOW_PROPS
@given(st.data(), amounts, st.integers(0, 2**32 - 1))
def test_7_planning_is_deterministic(data: st.DataObject, amount: float, seed: int) -> None:
    prof: CompiledProfile = data.draw(profile(all_active=True))
    p = data.draw(belief_for(len(prof.hypotheses)))
    kwargs = {
        "amount_paise": amount,
        "vulnerable": False,
        "phase": Phase.GUARDIAN_ACTIVE,
        "budgets": initial_budgets(prof),
    }
    a = plan_next(prof, p, rng=np.random.default_rng(seed), **kwargs)  # type: ignore[arg-type]
    b = plan_next(prof, p, rng=np.random.default_rng(seed), **kwargs)  # type: ignore[arg-type]
    assert a == b


@SLOW_PROPS
@given(st.data(), amounts, st.integers(0, 2**32 - 1))
def test_9_budgets_never_exceeded(data: st.DataObject, amount: float, seed: int) -> None:
    prof: CompiledProfile = data.draw(profile(all_active=True, max_sources=5))
    rng = np.random.default_rng(seed)
    p = data.draw(belief_for(len(prof.hypotheses)))
    budgets, observed, run = initial_budgets(prof), set[str](), list[str]()
    while True:
        plan = plan_next(
            prof,
            p,
            amount_paise=amount,
            vulnerable=False,
            phase=Phase.GUARDIAN_ACTIVE,
            observed=frozenset(observed),
            budgets=budgets,
            rng=rng,
        )
        if plan.is_stop:
            break
        src = prof.sources[plan.action]
        p = update(p, src.likelihood[int(rng.integers(len(src.outcomes)))])
        budgets, observed = budgets.after(src), observed | {plan.action}
        run.append(plan.action)
    srcs = [prof.sources[k] for k in run]
    assert len(srcs) <= (prof.max_active_probes or 0)
    assert sum(s.is_medium_plus for s in srcs) <= (prof.max_medium_plus or 0)
    groups = [s.exclusive_group for s in srcs if s.exclusive_group]
    assert len(groups) == len(set(groups))
    assert len(run) == len(set(run))


@PROPS
@given(st.data(), st.floats(0.0, 1.0))
def test_10_prior_construction(data: st.DataObject, q: float) -> None:
    prof: CompiledProfile = data.draw(profile())
    hyps = prof.hypotheses
    # a random subset of typologies is "known to the model"; the rest are KB-only
    known = [h for h in hyps[:-1] if h == hyps[0] or data.draw(st.booleans())]
    p_model = {h: data.draw(st.floats(0.01, 1.0)) for h in known}
    prior = build_prior(prof, p_model, q)
    u = open_mass(q, prof.open_mass_base, prof.open_mass_slope)
    kb_only = [h for h in hyps[:-1] if h not in p_model]
    w = sum(prof.open_mass_shares[h] for h in kb_only)
    assert prior.sum() == pytest.approx(1.0, abs=1e-12)
    assert np.all(prior >= 0)
    assert prior[hyps.index("UNEXPLAINED")] == pytest.approx(u * (1 - w), abs=1e-12)


def test_8_exploration_frequencies_match_propensities(guardian: CompiledProfile) -> None:
    """20,000 seeded draws of the exploration step: frequencies within 3σ of the logged
    propensities; never net-negative, never more friction than the greedy choice."""
    from delphi.engine.planner import explore
    from tests.golden.test_story_behaviour import KAMALA_PASSIVE, KAMALA_PRIOR

    p = np.asarray(KAMALA_PRIOR)
    for key, outcome in KAMALA_PASSIVE.items():
        src = guardian.sources[key]
        p = update(p, src.likelihood[src.outcomes.index(outcome)])
    observed = frozenset(KAMALA_PASSIVE)
    budgets: Budgets = initial_budgets(guardian)
    kwargs = {
        "amount_paise": 48_000_000,
        "vulnerable": False,
        "phase": Phase.GUARDIAN_ACTIVE,
        "observed": observed,
        "budgets": budgets,
    }
    base = plan_next(guardian, p, **kwargs)  # type: ignore[arg-type]
    assert not base.is_stop
    nets = {
        c.source_key: c.net_value_paise
        for c in base.candidates
        if c.eligible and c.net_value_paise is not None
    }
    greedy_tier = guardian.sources[base.action].tier_rank

    # integration: plan_next(rng) makes exactly the choice explore() makes with that seed
    for seed in range(300):
        plan = plan_next(guardian, p, rng=np.random.default_rng(seed), **kwargs)  # type: ignore[arg-type]
        expected = explore(guardian, nets, base.action, np.random.default_rng(seed))
        assert (plan.action, plan.selected_by, plan.propensity) == expected

    n = 20_000
    counts: dict[str, int] = {}
    propensity: dict[str, float] = {}
    for seed in range(n):
        action, selected_by, prop = explore(
            guardian, nets, base.action, np.random.default_rng(seed)
        )
        counts[action] = counts.get(action, 0) + 1
        propensity[action] = prop
        assert nets[action] >= 0  # never a net-negative probe
        assert guardian.sources[action].tier_rank <= greedy_tier  # never more friction
        if action != base.action:
            assert selected_by is SelectedBy.EXPLORE
    assert len(counts) > 1, "exploration never fired"
    for action, count in counts.items():
        expected_p = propensity[action]
        sigma = (expected_p * (1 - expected_p) / n) ** 0.5
        assert abs(count / n - expected_p) <= 3 * sigma + 1e-12, action
