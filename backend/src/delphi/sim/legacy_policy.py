"""The legacy policy, baseline B1, which decides the history period (docs/22 §2, docs/14 §4).

"DECLINE if the payee is LISTED_SUSPECT. HOLD (→ investigation) if amount ≥ ₹50,000 and the
payee is new; or the device is new and amount ≥ ₹10,000; or channel UPI_COLLECT_APPROVAL and
amount ≥ ₹25,000. Else RELEASE." In the history nothing investigates the holds, so a held or
declined payment never gets a label (selective labels, docs/14 §7).
"""

from __future__ import annotations

from delphi.sim.generator_config import LegacyPolicyConfig
from delphi.sim.labels import PaymentDecision

COLLECT_APPROVAL = "UPI_COLLECT_APPROVAL"
_PAISE_PER_INR = 100


def decide_b1(
    config: LegacyPolicyConfig,
    *,
    amount_paise: int,
    channel: str,
    payee_listed: bool,
    payee_is_new: bool,
    device_is_new: bool,
) -> PaymentDecision:
    if payee_listed:
        return PaymentDecision.DECLINED
    if (
        (payee_is_new and amount_paise >= config.hold_new_payee_min_inr * _PAISE_PER_INR)
        or (device_is_new and amount_paise >= config.hold_new_device_min_inr * _PAISE_PER_INR)
        or (
            channel == COLLECT_APPROVAL
            and amount_paise >= config.hold_collect_approval_min_inr * _PAISE_PER_INR
        )
    ):
        return PaymentDecision.HELD
    return PaymentDecision.RELEASED
