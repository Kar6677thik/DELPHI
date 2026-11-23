"""DELPHI and its ablations in the evaluation replay (docs/22 §2, #123 PR 3).

The policy runs the Guardian's own session procedure, ``guardian.api.run_session`` (#249), so the
harness scores exactly what the live Guardian does:
- **the prior** from the harness's sensing outputs (``delphi.evaluation.sensing``);
- **the passive probes**, classified by the live classifiers (``probes``) from the view, which
  carries what the live adapters read: the telemetry, the device, the registry, the payee's earlier
  payments, the payee's phone risk. The mule score is the docs/13 §6 fallback (no mule scorer in
  the harness);
- **the active probes**, answered by the customer through ``ask``: the customer's own stream (#123
  plan §3);
- **the guardrails**: the channel, the call-status consent, the question probes servable in the
  customer's language or the English fallback, and the fairness cap (the customer's earlier
  sessions with an active probe in the last 30 days, counted per policy run).

Ablations (docs/22 §2): ``DELPHI-myopic`` plans with lookahead depth 1 (ADR-0005), and
``DELPHI-no-active`` stops after the passive probes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from delphi.engine import SessionFacts
from delphi.engine.belief import kl_bits
from delphi.engine.exploration import session_seed
from delphi.engine.types import CompiledProfile
from delphi.evaluation.outcomes import Decision, ProbeMode, ProbeRecord
from delphi.evaluation.replay import Ask, PolicyDecision, PolicyView
from delphi.guardian.api import PriorInputs, run_session
from delphi.kb.model import KnowledgeBase
from delphi.probes.classifiers import (
    GUARDIAN_CLASSIFIERS,
    BeneficiaryIntelObs,
    CallStatusObs,
    DevicePostureObs,
    PhoneRiskObs,
    SessionBehaviourObs,
)
from delphi.probes.questions import FALLBACK_LANGUAGE, approved_variants

CALL_STATUS_CONSENT = "call_status"  # kb/sources/probes/CALL_STATUS.yaml requires_consent
FAIRNESS_WINDOW = timedelta(days=30)  # docs/06 §8: active sessions per customer in 30 days


def passive_outcomes(
    view: PolicyView, kb: KnowledgeBase, *, p_mule: float
) -> dict[str, str | None]:
    """Each passive probe's outcome, as the live adapters and classifiers give it (docs/08 §5).
    A probe whose input is missing (no telemetry) has no outcome (None: no update)."""
    t = kb.config.classifiers
    out: dict[str, str | None] = {}
    tel = view.telemetry
    out["BENEFICIARY_INTEL"] = GUARDIAN_CLASSIFIERS["BENEFICIARY_INTEL"].classify(
        BeneficiaryIntelObs(
            listed_in_suspect_registry=view.payee_listed,
            p_mule=p_mule,
            is_registered_merchant=view.payee_is_merchant,
            earlier_payments=view.payee_earlier_payments,
            earlier_distinct_customers=view.payee_earlier_customers,
            first_payment_at=view.payee_first_payment_at,
            as_of=view.initiated_at,
        ),
        t,
    )
    out["COUNTERPARTY_PHONE_RISK"] = GUARDIAN_CLASSIFIERS["COUNTERPARTY_PHONE_RISK"].classify(
        PhoneRiskObs(fri_category=view.payee_phone_risk), t
    )
    if tel is None:
        out |= {"DEVICE_POSTURE": None, "SESSION_BEHAVIOUR": None, "CALL_STATUS": None}
        return out
    out["DEVICE_POSTURE"] = GUARDIAN_CLASSIFIERS["DEVICE_POSTURE"].classify(
        DevicePostureObs(
            remote_access_active=tel.remote_access_active,
            screen_share_active=tel.screen_share_active,
            device_first_seen_at=view.device_first_seen_at
            or view.initiated_at - timedelta(days=3650),
            as_of=view.initiated_at,
        ),
        t,
    )
    out["SESSION_BEHAVIOUR"] = GUARDIAN_CLASSIFIERS["SESSION_BEHAVIOUR"].classify(
        SessionBehaviourObs(
            paste_on_account_field=tel.paste_on_account_field,
            max_field_dwell_s=tel.max_field_dwell_s,
            typing_cadence_z=tel.typing_cadence_z,
        ),
        t,
    )
    out["CALL_STATUS"] = (
        GUARDIAN_CLASSIFIERS["CALL_STATUS"].classify(
            CallStatusObs(
                on_call=tel.on_call, counterparty_in_contacts=tel.call_counterparty_known
            ),
            t,
        )
        if view.consent_call_status
        else None
    )
    return out


DECISIONS = {d.value: d for d in Decision}


@dataclass(slots=True)
class Delphi:
    """docs/22 §2's DELPHI (KB v1, frozen), and its ablations."""

    kb: KnowledgeBase
    profile: CompiledProfile
    sensing: Mapping[UUID, PriorInputs]
    p_mule_fallback: float
    name: str = "DELPHI"
    active: bool = True
    explore: bool = True
    _active_sessions: dict[UUID, list[datetime]] = field(default_factory=dict)

    @staticmethod
    def myopic(kb: KnowledgeBase, profile: CompiledProfile, **kw: Any) -> Delphi:
        return Delphi(kb, replace(profile, lookahead_depth=1), name="DELPHI-myopic", **kw)

    @staticmethod
    def no_active(kb: KnowledgeBase, profile: CompiledProfile, **kw: Any) -> Delphi:
        return Delphi(kb, profile, name="DELPHI-no-active", active=False, **kw)

    def _facts(self, view: PolicyView) -> SessionFacts:
        servable = frozenset(
            key
            for key, entry in self.kb.question_bank.items()
            if approved_variants(entry, view.language)
            or approved_variants(entry, FALLBACK_LANGUAGE)
        )
        recent = [
            t
            for t in self._active_sessions.get(view.customer_id, [])
            if view.initiated_at - t < FAIRNESS_WINDOW
        ]
        cap = self.kb.config.guardrails.max_active_sessions_per_customer_30d
        return SessionFacts(
            channel=view.channel,
            consents=frozenset({CALL_STATUS_CONSENT}) if view.consent_call_status else frozenset(),
            servable_question_probes=servable,
            fairness_cap_reached=len(recent) >= cap,
        )

    def decide(self, view: PolicyView, ask: Ask) -> PolicyDecision:
        result = run_session(
            self.profile,
            self.kb,
            self.sensing[view.payment_id],
            amount_paise=view.amount_paise,
            vulnerable=view.vulnerable,
            facts=self._facts(view),
            passive=passive_outcomes(view, self.kb, p_mule=self.p_mule_fallback),
            answer=ask,
            rng_seed=session_seed(str(view.payment_id), self.kb.config.engine.exploration_seed),
            explore=self.explore,
            active=self.active,
        )
        probes = tuple(
            ProbeRecord(
                s.probe_key,
                ProbeMode.ACTIVE if s.active else ProbeMode.PASSIVE,
                friction_paise=round(self.profile.sources[s.probe_key].cost_paise)
                if s.active
                else 0,
                info_yield_bits=(
                    kl_bits(s.belief_after, s.belief_before) if s.outcome is not None else None
                ),
            )
            for s in result.steps
        )
        if any(s.active for s in result.steps):
            self._active_sessions.setdefault(view.customer_id, []).append(view.initiated_at)
        posterior = dict(zip(self.profile.hypotheses, map(float, result.posterior), strict=True))
        return PolicyDecision(
            DECISIONS[result.decision],
            view.initiated_at,
            probes,
            argmax_hypothesis=max(posterior, key=posterior.__getitem__),
        )
