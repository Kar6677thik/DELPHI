"""The probes facade (docs/04 §4.1 rule 3): what other modules may use from ``delphi.probes``.

The Guardian runs the passive probes through their ports and the parallel runner, classifies
active responses, and serves question variants (docs/07 §9, docs/08 §2, §5). The Navigator's
evidence adapters build the observations its evidence rules classify (docs/09 §4).
"""

from __future__ import annotations

from delphi.probes.classifiers import (
    GUARDIAN_CLASSIFIERS,
    AnswerObs,
    BiometricObs,
    Classifier,
    CoolingOffObs,
    OtpObs,
)
from delphi.probes.evidence import (
    CRM_REPORTS,
    DEVICE_SHARING_WINDOW,
    DWELL_OUT_SHARE,
    DWELL_WINDOW,
    EVIDENCE_CLASSIFIERS,
    FAN_IN_WINDOW,
    GEO_VELOCITY_WINDOW,
    RECORDED_EVIDENCE,
    AccountProfileObs,
    ComplaintsObs,
    CrmObs,
    DeviceSharingObs,
    DwellObs,
    FanInObs,
    GeoVelocityObs,
    PayeeRelationshipObs,
    median_dwell,
)
from delphi.probes.passive import (
    Fetcher,
    PassiveRequest,
    PassiveResult,
    PassiveStatus,
    passive_executor,
    passive_fetchers,
    run_passive_probes,
)
from delphi.probes.questions import (
    NoApprovedVariantError,
    ServedQuestion,
    approved_variants,
    serve_question,
)
from delphi.probes.sim_adapters import (
    MuleScorePort,
    SimBeneficiaryIntel,
    SimPhoneRisk,
    SimSessionTelemetry,
)

__all__ = [
    "CRM_REPORTS",
    "DEVICE_SHARING_WINDOW",
    "DWELL_OUT_SHARE",
    "DWELL_WINDOW",
    "EVIDENCE_CLASSIFIERS",
    "FAN_IN_WINDOW",
    "GEO_VELOCITY_WINDOW",
    "GUARDIAN_CLASSIFIERS",
    "RECORDED_EVIDENCE",
    "AccountProfileObs",
    "AnswerObs",
    "BiometricObs",
    "Classifier",
    "ComplaintsObs",
    "CoolingOffObs",
    "CrmObs",
    "DeviceSharingObs",
    "DwellObs",
    "FanInObs",
    "Fetcher",
    "GeoVelocityObs",
    "MuleScorePort",
    "NoApprovedVariantError",
    "OtpObs",
    "PassiveRequest",
    "PassiveResult",
    "PassiveStatus",
    "PayeeRelationshipObs",
    "ServedQuestion",
    "SimBeneficiaryIntel",
    "SimPhoneRisk",
    "SimSessionTelemetry",
    "approved_variants",
    "median_dwell",
    "passive_executor",
    "passive_fetchers",
    "run_passive_probes",
    "serve_question",
]
