"""Scenario types and the hypothesis each one enacts, h(s) (docs/14 §4)."""

from __future__ import annotations

from enum import StrEnum


class ScenarioType(StrEnum):
    """In docs/14 §4 table order, which is also the order of the response-model freeze."""

    GENUINE_ROUTINE = "GENUINE_ROUTINE"
    GENUINE_UNUSUAL = "GENUINE_UNUSUAL"
    ATO = "ATO"
    APP_DIGITAL_ARREST = "APP_DIGITAL_ARREST"
    APP_INVESTMENT = "APP_INVESTMENT"
    MULE_FORWARDING = "MULE_FORWARDING"
    UPI_COLLECT_REFUND = "UPI_COLLECT_REFUND"


# h(s): the ground-truth hypothesis key each scenario enacts (docs/14 §4). The hidden
# collect-refund scenario enacts APP_COLLECT_REFUND, the key the S-4 script promotes (docs/11
# §7); it is not in KB v1, so its answers are explicit (docs/14 §5.2).
SCENARIO_HYPOTHESIS: dict[ScenarioType, str] = {
    ScenarioType.GENUINE_ROUTINE: "GENUINE",
    ScenarioType.GENUINE_UNUSUAL: "GENUINE",
    ScenarioType.ATO: "ATO",
    ScenarioType.APP_DIGITAL_ARREST: "APP_DIGITAL_ARREST",
    ScenarioType.APP_INVESTMENT: "APP_INVESTMENT",
    ScenarioType.MULE_FORWARDING: "MULE",
    ScenarioType.UPI_COLLECT_REFUND: "APP_COLLECT_REFUND",
}

# The scenario whose answers stand for P_true(GENUINE) in the coached-victim mix and the
# coaching-adaptation target (docs/14 §5.2, §5.3).
GENUINE_REFERENCE = ScenarioType.GENUINE_ROUTINE


def ground_truth_key(scenario: ScenarioType) -> str:
    """The hypothesis key ``sim_truth`` records for a scenario (docs/14 §4)."""
    return SCENARIO_HYPOTHESIS[scenario]
