"""The probes facade (docs/04 §4.1 rule 3): what other modules may use from ``delphi.probes``.

The Guardian runs the passive probes through their ports and the parallel runner, classifies
active responses, and serves question variants (docs/07 §9, docs/08 §2, §5).
"""

from __future__ import annotations

from delphi.probes.classifiers import GUARDIAN_CLASSIFIERS, Classifier
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
    "GUARDIAN_CLASSIFIERS",
    "Classifier",
    "Fetcher",
    "MuleScorePort",
    "NoApprovedVariantError",
    "PassiveRequest",
    "PassiveResult",
    "PassiveStatus",
    "ServedQuestion",
    "SimBeneficiaryIntel",
    "SimPhoneRisk",
    "SimSessionTelemetry",
    "approved_variants",
    "passive_executor",
    "passive_fetchers",
    "run_passive_probes",
    "serve_question",
]
