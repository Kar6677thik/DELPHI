"""Immutable KB model types (docs/07-knowledge-base.md).

This module depends only on pydantic and the standard library (docs/04 §4.1 rule 2).
Structural checks that need only one object live here as validators; cross-object rules
(docs/07 §3) live in ``delphi.kb.validate``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

UPPER_SNAKE = r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$"
Key = Annotated[str, StringConstraints(pattern=UPPER_SNAKE)]

GENUINE = "GENUINE"
UNEXPLAINED = "UNEXPLAINED"

FrictionTier = Literal["NONE", "LOW", "MEDIUM", "HIGH"]
TIER_RANK: dict[str, int] = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
Context = Literal["OUTBOUND_PAYMENT", "ACCOUNT_REVIEW"]
F3Stage = Literal["INITIAL_ACCESS", "POSITIONING", "EXECUTION", "MONETIZATION"]
Language = Literal["en", "hi", "te", "ta"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- building blocks


class LikelihoodRow(_Frozen):
    """One Dirichlet row: P(outcome | hypothesis) as probabilities plus an ESS (docs/06 §2)."""

    probs: tuple[float, ...]
    ess: float

    @property
    def alpha(self) -> tuple[float, ...]:
        return tuple(p * self.ess for p in self.probs)


class LossCell(_Frozen):
    """Loss(d, h) = (base_inr * 100 + rate * A) * m  (docs/06 §4).

    YAML form: ``[base_inr, rate, vulnerability_scaled]``.
    """

    base_inr: float = Field(ge=0)
    rate: float = Field(ge=0)
    vulnerability_scaled: bool

    @model_validator(mode="before")
    @classmethod
    def _from_list(cls, value: Any) -> Any:
        if isinstance(value, list | tuple):
            if len(value) != 3:  # noqa: PLR2004 - the YAML triple
                raise ValueError("loss cell must be [base_inr, rate, vulnerability_scaled]")
            return {"base_inr": value[0], "rate": value[1], "vulnerability_scaled": value[2]}
        return value


LossTable = dict[Key, dict[Key, LossCell]]  # hypothesis or profile -> decision -> cell


# --------------------------------------------------------------------------- sources


class ObservationSource(_Frozen):
    """A probe (Guardian) or an evidence item (Navigator) with a categorical outcome."""

    key: Key
    kind: Literal["PROBE", "EVIDENCE"]
    outcomes: tuple[Key, ...]
    contexts: tuple[Context, ...]
    enabled: bool
    classifier: Key
    description: str
    # probes
    mode: Literal["PASSIVE", "ACTIVE"] | None = None
    friction_tier: FrictionTier = "NONE"
    channels: tuple[Key, ...] | None = None
    exclusive_group: Key | None = None
    requires_consent: str | None = None
    question: bool = False
    answer_options: tuple[Key, ...] | None = None
    expected_latency_s: float | None = Field(default=None, ge=0)
    # evidence
    subject: Literal["COUNTERPARTY", "PAYER", "PAYER_TO_COUNTERPARTY"] | None = None
    expected_minutes: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _kind_fields(self) -> ObservationSource:
        if len(self.outcomes) < 2 or len(set(self.outcomes)) != len(self.outcomes):  # noqa: PLR2004
            raise ValueError("outcomes must be at least 2 distinct keys")
        if not self.contexts:
            raise ValueError("contexts must not be empty")
        if self.kind == "PROBE":
            if self.mode is None or self.expected_latency_s is None:
                raise ValueError("probes need mode and expected_latency_s")
            if self.subject is not None or self.expected_minutes is not None:
                raise ValueError("probes must not set subject/expected_minutes")
            if self.mode == "PASSIVE" and self.friction_tier != "NONE":
                raise ValueError("passive probes must have friction_tier NONE")
            if self.question != (self.answer_options is not None):
                raise ValueError("question probes (and only they) declare answer_options")
        else:
            if self.subject is None or self.expected_minutes is None:
                raise ValueError("evidence needs subject and expected_minutes")
            if self.mode is not None or self.question or self.exclusive_group is not None:
                raise ValueError("evidence must not set probe-only fields")
        return self


# --------------------------------------------------------------------------- hypotheses


class Hypothesis(_Frozen):
    key: Key
    display_name: str
    kind: Literal["GENUINE", "TYPOLOGY"]
    contexts: tuple[Context, ...]
    f3_stages: tuple[F3Stage, ...]
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]
    open_mass_share: float | None = None
    description: str
    likelihoods: dict[Key, LikelihoodRow]
    loss: LossTable = Field(default_factory=dict)  # typologies only: profile -> decision -> cell


# --------------------------------------------------------------------------- decision profiles


class DecisionProfile(_Frozen):
    key: Key
    context: Context
    source_kind: Literal["PROBE", "EVIDENCE"]
    amount_basis: Literal["PAYMENT_AMOUNT", "CREDITS_7D"]
    description: str
    decisions: tuple[Key, ...]
    loss: LossTable  # GENUINE and UNEXPLAINED cells only

    @model_validator(mode="after")
    def _decisions_distinct(self) -> DecisionProfile:
        if len(self.decisions) < 2 or len(set(self.decisions)) != len(self.decisions):  # noqa: PLR2004
            raise ValueError("decisions must be at least 2 distinct keys")
        return self


# --------------------------------------------------------------------------- question bank


class QuestionOption(_Frozen):
    key: Key
    text: str


class QuestionVariant(_Frozen):
    variant_id: str
    language: Language
    status: Literal["APPROVED", "PENDING_REVIEW", "REJECTED"]
    text: str
    options: tuple[QuestionOption, ...]


class QuestionBankEntry(_Frozen):
    probe_key: Key
    variants: tuple[QuestionVariant, ...]


# --------------------------------------------------------------------------- engine.yaml


class LookaheadDepth(_Frozen):
    guardian: int = Field(ge=1, le=5)
    navigator: int = Field(ge=1, le=5)


class FastPath(_Frozen):
    min_p_genuine: float = Field(gt=0, le=1)
    max_amount_inr: float = Field(ge=0)
    max_anomaly_percentile: float = Field(ge=0, le=1)


class EngineSection(_Frozen):
    lookahead_depth: LookaheadDepth
    stop_epsilon_paise: float = Field(ge=0)
    exploration_epsilon: float = Field(ge=0, le=1)
    exploration_seed: int
    vulnerability_multiplier: float = Field(ge=1)
    vulnerable_age_threshold: int
    open_mass_base: float = Field(ge=0, lt=1)
    open_mass_slope: float = Field(ge=0, lt=1)
    unexplained_routing_threshold: float = Field(gt=0, le=1)
    fast_path: FastPath

    @model_validator(mode="after")
    def _open_mass_below_one(self) -> EngineSection:
        if self.open_mass_base + self.open_mass_slope >= 1:
            raise ValueError("open_mass_base + open_mass_slope must be < 1")
        return self


class Guardrails(_Frozen):
    max_active_probes_per_session: int = Field(ge=0)
    max_medium_plus_per_session: int = Field(ge=0)
    max_active_sessions_per_customer_30d: int = Field(ge=0)
    passive_adapter_timeout_ms: int = Field(gt=0)
    active_probe_response_timeout_s: int = Field(gt=0)
    cooling_off_extra_timeout_s: int = Field(ge=0)
    session_max_duration_s: int = Field(gt=0)


class Costs(_Frozen):
    friction_cost_inr: dict[FrictionTier, float]
    delay_cost_inr_per_s: float = Field(ge=0)
    investigator_cost_inr_per_min: float = Field(ge=0)

    @model_validator(mode="after")
    def _all_tiers(self) -> Costs:
        if set(self.friction_cost_inr) != set(TIER_RANK):
            raise ValueError("friction_cost_inr must define NONE, LOW, MEDIUM and HIGH")
        return self


class ClassifierThresholds(_Frozen):
    mule_score_high: float
    established_payee_min_payments: int
    established_payee_min_distinct_customers: int
    established_payee_min_age_days: int
    new_device_days: int
    paste_or_dwell_max_field_dwell_s: float
    cadence_mismatch_z: float
    purpose_fast_s: float
    purpose_slow_s: float
    spell_breaker_hesitation_s: float
    otp_max_attempts: int
    cooling_off_window_s: float
    cooling_off_send_now_presses: int
    fan_in_high: int
    fan_in_moderate: int
    dwell_fast_min: float
    dwell_slow_h: float
    new_account_days: int
    crm_lookback_days: int
    impossible_travel_kmh: float
    small_payment_inr: float


class NavigatorSection(_Frozen):
    mule_sweep_threshold: float
    mule_sweep_interval_min: float
    campaign_window_days: int


class VisibilityLag(_Frozen):
    demo: float
    eval: float


class ChaseSection(_Frozen):
    min_trace_inr: float
    freeze_latency_internal_s: float
    freeze_latency_external_min: float
    dwell_fallback_mule_min: float
    dwell_fallback_other_h: float
    lawful_hold_kappa: float = Field(ge=0, le=1)
    wrongful_base_inr: float
    wrongful_rate: float
    max_liens_per_round: int
    retrace_interval_min: float
    ticket_expiry_min: float
    visibility_lag_min: VisibilityLag


class ScoutSection(_Frozen):
    min_cluster_size: int
    min_samples: int
    lookback_days: int
    auto_trigger_new_items: int
    shadow_min_sessions: int
    shadow_min_sessions_demo: int


class TimeSection(_Frozen):
    demo_time_scale: float = Field(gt=0, le=1)


class Elicitation(_Frozen):
    default_ess: float
    confidence_ess: dict[Literal["LOW", "MEDIUM", "HIGH"], float]


class EngineConfig(_Frozen):
    engine: EngineSection
    guardrails: Guardrails
    costs: Costs
    classifiers: ClassifierThresholds
    navigator: NavigatorSection
    chase: ChaseSection
    scout: ScoutSection
    time: TimeSection
    elicitation: Elicitation


# --------------------------------------------------------------------------- regulatory.yaml


class AccountReviewWindows(_Frozen):
    customer_response_window_days: int
    bank_decision_window_days: int
    max_debit_hold_days: int


class CaseSla(_Frozen):
    hold_review_sla_minutes: float
    protective_sla_minutes: float
    account_review_sla_minutes: float


class ChaseRegulatory(_Frozen):
    golden_hour_minutes: float
    lien_review_interval_days: int


class Regulatory(_Frozen):
    profile: Key
    account_review: AccountReviewWindows
    case_sla: CaseSla
    chase: ChaseRegulatory


# --------------------------------------------------------------------------- the bundle


class KnowledgeBase(_Frozen):
    """A fully resolved KB version. Immutable; hashed canonically (docs/07 §1)."""

    kb_format_version: Literal[1]
    name: str
    description: str
    hypothesis_order: tuple[Key, ...]
    source_order: tuple[Key, ...]
    hypotheses: dict[Key, Hypothesis]
    sources: dict[Key, ObservationSource]
    decision_profiles: dict[Key, DecisionProfile]
    question_bank: dict[Key, QuestionBankEntry]
    config: EngineConfig
    regulatory: Regulatory

    def ordered_hypotheses(self) -> tuple[Hypothesis, ...]:
        return tuple(self.hypotheses[k] for k in self.hypothesis_order)

    def ordered_sources(self) -> tuple[ObservationSource, ...]:
        return tuple(self.sources[k] for k in self.source_order)
