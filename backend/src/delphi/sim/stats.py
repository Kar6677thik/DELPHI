"""The dataset stats report (docs/25 M2 deliverable; FR-SIM-02, #17).

For every scenario, customer sessions per 10,000 in the history and in the live/eval window,
against the configured rate (docs/14 §4). A session is a customer payment through the app:
genuine, a fraud victim's, a home-bank mule's onward transfer. The window holds no
MULE_FORWARDING sessions: those follow from what each policy releases, so the evaluation replay
makes them (docs/22 §1). Its rate is checked on the history, which runs "the same scenarios at
the same rates". Also: B1's hold and decline shares, label coverage, fraud and complaint
counts, and the share of credited mules the registry lists.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from delphi.sim.dataset import Dataset
from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.history import Payment
from delphi.sim.scenarios import ScenarioType

_PER_10K = 10_000


@dataclass(frozen=True, slots=True)
class ScenarioRate:
    scenario: ScenarioType
    configured_per_10k: float
    history_per_10k: float
    window_per_10k: float | None  # None without a window


@dataclass(frozen=True, slots=True)
class StatsReport:
    profile: str
    history_sessions: int
    window_sessions: int
    rates: tuple[ScenarioRate, ...]
    held_share: float
    declined_share: float
    label_coverage: float  # history sessions with at least one scheduled label
    fraud_payments: int
    complaints: int
    mule_listing_share: float  # listed / credited mule accounts

    def rate(self, scenario: ScenarioType) -> ScenarioRate:
        return next(r for r in self.rates if r.scenario is scenario)


def configured_rates(config: GeneratorConfig) -> dict[ScenarioType, float]:
    """docs/14 §4's rates per 10,000 payments, as configured."""
    unusual = config.genuine_unusual
    rates = {
        ScenarioType.GENUINE_ROUTINE: unusual.routine_rate_per_10k,
        ScenarioType.GENUINE_UNUSUAL: unusual.rate_per_10k,
        **{ScenarioType(s): r for s, r in config.fraud.rate_per_10k.items()},
        ScenarioType.MULE_FORWARDING: config.forwarding.rate_per_10k,
        ScenarioType.UPI_COLLECT_REFUND: config.collect_refund.rate_per_10k,
    }
    return {s: rates[s] for s in ScenarioType}


def _per_10k(counts: Counter[ScenarioType], total: int) -> dict[ScenarioType, float]:
    return {s: _PER_10K * counts[s] / total if total else 0.0 for s in ScenarioType}


def dataset_stats(dataset: Dataset, config: GeneratorConfig) -> StatsReport:
    history, window = dataset.history, dataset.history.window
    kinds = {i.id: ScenarioType(i.scenario_type) for i in history.scenario_instances}
    truth = {
        t.payment_id: t for t in (*history.payment_truth, *(window.payment_truth if window else ()))
    }

    def scenario(p: Payment) -> ScenarioType:
        return kinds[truth[p.id].scenario_instance_id]

    sessions = [p for p in history.payments if p.device_id is not None]
    window_sessions = list(window.payments) if window else []
    in_history = _per_10k(Counter(scenario(p) for p in sessions), len(sessions))
    in_window = _per_10k(Counter(scenario(p) for p in window_sessions), len(window_sessions))
    rates = tuple(
        ScenarioRate(s, rate, in_history[s], in_window[s] if window_sessions else None)
        for s, rate in configured_rates(config).items()
    )
    labelled = {label.subject_id for label in history.labels}
    statuses = Counter(p.status for p in sessions)
    mules = {a.id for a in dataset.network.accounts}
    credited = {
        p.payee_account_id
        for p in history.payments
        if p.payee_account_id in mules and p.status == "EXECUTED"
    }
    n = len(sessions) or 1
    return StatsReport(
        profile=dataset.population.profile,
        history_sessions=len(sessions),
        window_sessions=len(window_sessions),
        rates=rates,
        held_share=statuses["HELD"] / n,
        declined_share=statuses["DECLINED"] / n,
        label_coverage=sum(str(p.id) in labelled for p in sessions) / n,
        fraud_payments=sum(t.is_fraud for t in truth.values()),
        complaints=len(history.complaints),
        mule_listing_share=len(history.registry_listings) / len(credited) if credited else 0.0,
    )


def format_report(report: StatsReport) -> str:
    def cell(value: float | None) -> str:
        return "-" if value is None else f"{value:9.1f}"

    lines = [
        f"profile {report.profile}: {report.history_sessions} history sessions, "
        f"{report.window_sessions} window sessions",
        "",
        f"{'scenario':<22}{'configured':>11}{'history':>10}{'window':>10}   (per 10,000)",
    ]
    lines += [
        f"{r.scenario.value:<22}{r.configured_per_10k:>11.1f}{cell(r.history_per_10k):>10}"
        f"{cell(r.window_per_10k):>10}"
        for r in report.rates
    ]
    lines += [
        "",
        f"B1 held {report.held_share:.1%}, declined {report.declined_share:.1%}; "
        f"label coverage {report.label_coverage:.1%}",
        f"fraud payments {report.fraud_payments}, complaints {report.complaints}, "
        f"credited mules listed {report.mule_listing_share:.1%}",
    ]
    return "\n".join(lines)
