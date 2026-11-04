"""Frozen response models (docs/14 §5, FR-SIM-03)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from delphi.cli import main
from delphi.kb.model import KnowledgeBase
from delphi.sim.response_models import (
    GENERATED_FILE,
    CoachingAdaptation,
    ResponseModelConfig,
    TelemetryTargets,
    answered_sources,
    draw_signals,
    dump_yaml,
    freeze,
    load_config,
    load_response_models,
    row_rng,
)
from delphi.sim.scenarios import SCENARIO_HYPOTHESIS, ScenarioType, ground_truth_key

DA = ScenarioType.APP_DIGITAL_ARREST
INV = ScenarioType.APP_INVESTMENT
ROUTINE = ScenarioType.GENUINE_ROUTINE
UNUSUAL = ScenarioType.GENUINE_UNUSUAL
REFUND = ScenarioType.UPI_COLLECT_REFUND
ACTIVE_PROBES = (
    "OTP_STEP_UP",
    "BIOMETRIC_STEP_UP",
    "PURPOSE_QUESTION",
    "SPELL_BREAKER_DIGITAL_ARREST",
    "SPELL_BREAKER_INVESTMENT",
    "COOLING_OFF",
    "COLLECT_INTENT_CHECK",
)


@pytest.fixture(scope="module")
def config() -> ResponseModelConfig:
    return load_config()


def test_committed_freeze_matches_kb_v1_and_config(
    kb: KnowledgeBase, config: ResponseModelConfig
) -> None:
    """The snapshot test (docs/21 §1): the committed file is exactly what freezing yields."""
    assert GENERATED_FILE.read_text(encoding="utf-8") == dump_yaml(freeze(kb, config)), (
        "response_models.generated.yaml is stale: run `uv run delphi sim "
        "freeze-response-models`, bump generator_version and record it in CHANGELOG.md"
    )


def test_freeze_is_deterministic(kb: KnowledgeBase, config: ResponseModelConfig) -> None:
    assert dump_yaml(freeze(kb, config)) == dump_yaml(freeze(kb, config))


def test_a_different_seed_gives_different_answers(
    kb: KnowledgeBase, config: ResponseModelConfig
) -> None:
    other = config.model_copy(update={"freeze_seed": 100})
    assert freeze(kb, other).answers[DA] != freeze(kb, config).answers[DA]


def test_answers_cover_active_probes_and_outreach(
    kb: KnowledgeBase, config: ResponseModelConfig
) -> None:
    assert answered_sources(kb, config) == (*ACTIVE_PROBES, "CUSTOMER_OUTREACH_CALL")
    frozen = freeze(kb, config)
    assert tuple(frozen.answers) == tuple(ScenarioType)
    for rows in frozen.answers.values():
        assert set(rows) == set(answered_sources(kb, config))
        for key, probs in rows.items():
            assert len(probs) == len(kb.sources[key].outcomes)
            assert min(probs) >= 0.0
            assert sum(probs) == pytest.approx(1.0, abs=1e-12)


def test_answers_are_perturbed_not_copied_from_the_kb(
    kb: KnowledgeBase, config: ResponseModelConfig
) -> None:
    """docs/14 §1 rule 3: the simulator's truth is not the KB, but stays near it."""
    frozen = freeze(kb, config)
    for scenario in (ROUTINE, ScenarioType.ATO, DA, INV, ScenarioType.MULE_FORWARDING):
        hypothesis = SCENARIO_HYPOTHESIS[scenario]
        for key in ACTIVE_PROBES:
            drawn = np.asarray(frozen.answers[scenario][key])
            kb_row = np.asarray(kb.hypotheses[hypothesis].likelihoods[key].probs)
            distance = float(np.abs(drawn - kb_row).sum())
            assert 0.0 < distance < 0.5, (scenario, key, distance)


def test_collect_refund_answers_are_exactly_as_specified(
    kb: KnowledgeBase, config: ResponseModelConfig
) -> None:
    rows = freeze(kb, config).answers[REFUND]
    assert rows["PURPOSE_QUESTION"] == (0.15, 0.03, 0.12, 0.70)
    assert rows["SPELL_BREAKER_DIGITAL_ARREST"] == (0.10, 0.02, 0.88)
    assert rows["SPELL_BREAKER_INVESTMENT"] == (0.10, 0.02, 0.88)
    assert rows["OTP_STEP_UP"] == rows["BIOMETRIC_STEP_UP"] == (0.97, 0.03)
    assert rows["COOLING_OFF"] == (0.15, 0.25, 0.60)
    assert rows["COLLECT_INTENT_CHECK"] == (0.88, 0.12)
    # Same as APP_INVESTMENT: the victim realises once a verified bank caller explains.
    assert rows["CUSTOMER_OUTREACH_CALL"] == (0.50, 0.25, 0.05, 0.20)


def test_ground_truth_keys(kb: KnowledgeBase) -> None:
    """The hidden scenario enacts APP_COLLECT_REFUND, the key S-4 promotes (docs/11 §7)."""
    assert ground_truth_key(REFUND) == "APP_COLLECT_REFUND"
    assert "APP_COLLECT_REFUND" not in kb.hypotheses  # unknown to KB v1
    assert ground_truth_key(DA) == "APP_DIGITAL_ARREST"
    assert ground_truth_key(ScenarioType.MULE_FORWARDING) == "MULE"


def test_a_missing_explicit_row_is_an_error(kb: KnowledgeBase, config: ResponseModelConfig) -> None:
    rows = dict(config.explicit_answers[REFUND])
    del rows["CUSTOMER_OUTREACH_CALL"]
    broken = config.model_copy(update={"explicit_answers": {REFUND: rows}})
    with pytest.raises(ValueError, match="no CUSTOMER_OUTREACH_CALL row"):
        freeze(kb, broken)


def test_a_scenario_without_kb_or_explicit_rows_is_an_error(
    kb: KnowledgeBase, config: ResponseModelConfig
) -> None:
    broken = config.model_copy(update={"explicit_answers": {}})
    with pytest.raises(ValueError, match="APP_COLLECT_REFUND has no KB row"):
        freeze(kb, broken)


def test_each_row_has_its_own_name_derived_stream(
    kb: KnowledgeBase, config: ResponseModelConfig
) -> None:
    """Adding a scenario or a source later must not move the numbers already frozen."""
    frozen = freeze(kb, config)
    alpha = config.concentration * np.asarray(
        kb.hypotheses["APP_DIGITAL_ARREST"].likelihoods["COOLING_OFF"].probs
    )
    alone = row_rng(config.freeze_seed, DA, "COOLING_OFF").dirichlet(alpha)
    assert frozen.answers[DA]["COOLING_OFF"] == tuple(float(p) for p in alone)
    # Without the outreach evidence (one source fewer), every other row is unchanged.
    fewer = freeze(kb, config.model_copy(update={"perturbed_evidence": ()}))
    for scenario in ScenarioType:
        for key in ACTIVE_PROBES:
            assert fewer.answers[scenario][key] == frozen.answers[scenario][key]


def test_genuine_unusual_purpose_timing_is_pinned(
    kb: KnowledgeBase, config: ResponseModelConfig
) -> None:
    unpinned = freeze(kb, config.model_copy(update={"pinned_outcomes": {}}))
    pinned = freeze(kb, config)
    before = unpinned.answers[UNUSUAL]["PURPOSE_QUESTION"]
    after = pinned.answers[UNUSUAL]["PURPOSE_QUESTION"]
    timing = kb.sources["PURPOSE_QUESTION"].outcomes.index("TIMING_ANOMALOUS")
    assert after[timing] == 0.12
    others = [i for i in range(len(after)) if i != timing]
    ratios = {after[i] / before[i] for i in others}
    assert max(ratios) == pytest.approx(min(ratios))  # proportions kept
    assert sum(after) == pytest.approx(1.0)
    # Pinning one row draws nothing extra: every other row is unchanged.
    assert pinned.answers[UNUSUAL]["COOLING_OFF"] == unpinned.answers[UNUSUAL]["COOLING_OFF"]


def test_coached_victims_mix_genuine_and_own_answers(
    kb: KnowledgeBase, config: ResponseModelConfig
) -> None:
    frozen = freeze(kb, config)
    assert set(frozen.coached) == {DA, INV}
    for scenario in (DA, INV):
        for probe in (
            "PURPOSE_QUESTION",
            "SPELL_BREAKER_DIGITAL_ARREST",
            "SPELL_BREAKER_INVESTMENT",
        ):
            own = np.asarray(frozen.answers[scenario][probe])
            genuine = np.asarray(frozen.answers[ROUTINE][probe])
            np.testing.assert_allclose(
                frozen.coached[scenario][probe], 0.5 * genuine + 0.5 * own, rtol=0, atol=1e-15
            )
        assert "COOLING_OFF" not in frozen.coached[scenario]


def test_loaded_models_equal_the_freeze(kb: KnowledgeBase, config: ResponseModelConfig) -> None:
    frozen = freeze(kb, config)
    models = load_response_models()
    assert models.answers == frozen.answers  # floats survive the YAML round trip exactly
    assert models.coached == frozen.coached
    assert models.coached_share == 0.30
    assert models.adaptations == ()
    assert models.answer_probs(DA, "PURPOSE_QUESTION") == frozen.answers[DA]["PURPOSE_QUESTION"]
    assert (
        models.answer_probs(DA, "PURPOSE_QUESTION", coached=True)
        == (frozen.coached[DA]["PURPOSE_QUESTION"])
    )
    # Coaching applies only to the coached probes.
    assert models.answer_probs(DA, "COOLING_OFF", coached=True) == frozen.answers[DA]["COOLING_OFF"]


def test_sample_answer_follows_the_distribution() -> None:
    models = load_response_models()
    rng = np.random.Generator(np.random.PCG64(7))
    n = 20_000
    draws = [models.sample_answer(rng, REFUND, "PURPOSE_QUESTION") for _ in range(n)]
    share = draws.count("BELIEVES_RECEIVING_MONEY") / n
    assert share == pytest.approx(0.70, abs=0.015)


def test_coaching_adaptation_moves_answers_toward_genuine_over_the_ramp() -> None:
    switch = CoachingAdaptation(
        scenario=DA, probe="SPELL_BREAKER_DIGITAL_ARREST", start_day=10, shift=0.5
    )
    assert switch.ramp_days == 14  # the docs/14 §5.3 example
    assert switch.weight(0) == 0.0
    assert switch.weight(10) == 0.0
    assert switch.weight(17) == pytest.approx(0.25)
    assert switch.weight(24) == pytest.approx(0.5)
    assert switch.weight(100) == pytest.approx(0.5)

    models = load_response_models()
    switched = replace(models, adaptations=(switch,))
    probe = "SPELL_BREAKER_DIGITAL_ARREST"
    own = np.asarray(models.answers[DA][probe])
    genuine = np.asarray(models.answers[ROUTINE][probe])
    np.testing.assert_allclose(switched.answer_probs(DA, probe, sim_day=5), own)
    np.testing.assert_allclose(
        switched.answer_probs(DA, probe, sim_day=24), 0.5 * genuine + 0.5 * own
    )
    yes = 1  # ANSWERED_YES: the signal the coaching erodes
    trend = [switched.answer_probs(DA, probe, sim_day=d)[yes] for d in range(10, 26)]
    assert trend == sorted(trend, reverse=True)
    # Other scenarios and probes are untouched.
    assert switched.answer_probs(INV, probe, sim_day=24) == models.answers[INV][probe]
    assert switched.answer_probs(DA, "COOLING_OFF", sim_day=24) == models.answers[DA]["COOLING_OFF"]


def test_coaching_adaptation_is_off_by_default(config: ResponseModelConfig) -> None:
    assert config.coaching_adaptation == ()


def test_telemetry_targets_follow_the_spec_table(config: ResponseModelConfig) -> None:
    targets = config.telemetry_targets
    assert set(targets) == set(ScenarioType)
    assert targets[DA] == TelemetryTargets(
        on_call_unknown_number=0.80,
        remote_access_active=0.25,
        paste_or_long_dwell=0.50,
        cadence_mismatch=0.04,
    )
    assert targets[UNUSUAL].paste_or_long_dwell == 0.20  # "SESSION_BEHAVIOUR hesitant 0.20"


def test_draw_signals_hits_the_target_rates(config: ResponseModelConfig) -> None:
    targets = config.telemetry_targets[ScenarioType.ATO]
    rng = np.random.Generator(np.random.PCG64(11))
    n = 20_000
    draws = [draw_signals(rng, targets) for _ in range(n)]
    for name in (
        "on_call_unknown_number",
        "remote_access_active",
        "paste_or_long_dwell",
        "cadence_mismatch",
    ):
        rate = sum(getattr(d, name) for d in draws) / n
        assert rate == pytest.approx(getattr(targets, name), abs=0.015), name


def test_cli_checks_and_writes_the_freeze(tmp_path: Path) -> None:
    out = tmp_path / "frozen.yaml"
    assert main(["sim", "freeze-response-models", "--check", "--out", str(out)]) == 1
    assert main(["sim", "freeze-response-models", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == GENERATED_FILE.read_text(encoding="utf-8")
    assert main(["sim", "freeze-response-models", "--check", "--out", str(out)]) == 0
    assert main(["sim", "freeze-response-models", "--check"]) == 0


def test_cli_refuses_an_invalid_kb(tmp_path: Path) -> None:
    assert main(["sim", "freeze-response-models", "--kb", str(tmp_path)]) == 1
