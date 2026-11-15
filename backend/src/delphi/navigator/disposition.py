"""Disposing of a case (docs/05 §4, docs/09 §2 item 6, docs/16 §2, FR-NAV-07, #103).

An investigator decides a case, in one transaction:

- the decision must be one of the case profile's (RELEASE/CONFIRM_FRAUD for a payment hold,
  CLEAR/CONFIRM_MULE for an account review); CONFIRM_FRAUD names a fraud typology that applies to
  the case; the self-reported confidence is 1–5;
- the engine's recommendation is computed from the posterior at this moment: a disposition
  that differs from it is an **override** and needs a rationale of at least 20 characters;
- optimistic locking: the caller's ``If-Match`` version must be the case's (412 otherwise), and a
  DISPOSED case is immutable (409);
- the held payment moves on (docs/05 §3): RELEASE executes it, CONFIRM_FRAUD cancels it;
- the bank now knows a label (docs/14 §7, ``INVESTIGATOR_DISPOSITION``): the confirmed typology,
  MULE for a confirmed mule, GENUINE for a release or a clear;
- an audit entry and a ``case.updated`` event.

The disposition is the investigator's: the engine only recommends (ADR-0004).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from delphi.engine.risk import decide, loss_matrix, risk_by_decision
from delphi.kb.model import UNEXPLAINED
from delphi.kb.store import get_version
from delphi.navigator.console import belief_vector, case_profile
from delphi.navigator.store import CaseRecord, CaseStatus, CaseType, get_case
from delphi.navigator.tables import cases
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.events import EventType, publish
from delphi.platform.labels import LabelRow, record_labels
from delphi.sim import api as bank

MIN_OVERRIDE_RATIONALE = 20  # docs/05 §4: an override's rationale has at least 20 characters
CONFIDENCE_RANGE = range(1, 6)  # docs/09 §2: self-reported confidence 1–5
GENUINE = "GENUINE"
MULE = "MULE"
FRAUD_OTHER = "FRAUD_OTHER"  # docs/05 §4, §8: a fraud the KB does not model (#176)
INVESTIGATOR_DISPOSITION = "INVESTIGATOR_DISPOSITION"  # docs/14 §7 label source
FRAUD_DECISIONS = {"CONFIRM_FRAUD", "CONFIRM_MULE"}


@dataclass(frozen=True, slots=True)
class Disposition:
    decision: str
    confidence: int
    typology: str | None = None
    note: str | None = None
    override_rationale: str | None = None


def _invalid(message: str) -> DelphiError:
    return DelphiError(message, code=ProblemCode.VALIDATION_FAILED)


def vulnerable_payer(session: Session, case: CaseRecord) -> bool:
    """The loss weighting of doc 06 §4: whether the payment's payer is a vulnerable customer."""
    if case.subject_payment_id is None:
        return False
    payment = bank.get_payment(session, case.subject_payment_id)
    if payment is None or payment.payer_customer_id is None:
        return False
    return bool(
        session.execute(
            select(bank.customers.c.is_vulnerable).where(
                bank.customers.c.id == payment.payer_customer_id
            )
        ).scalar_one_or_none()
    )


def dispose(
    session: Session,
    clock: Clock,
    case_id: UUID,
    disposition: Disposition,
    *,
    expected_version: int,
    user_id: UUID,
) -> CaseRecord:
    case = get_case(session, case_id)
    if case is None:
        raise NotFoundError(f"No case {case_id}.")
    session.execute(select(cases.c.id).where(cases.c.id == case_id).with_for_update())
    case = get_case(session, case_id)
    assert case is not None
    if case.status is CaseStatus.DISPOSED:
        raise DelphiError(
            "This case is already disposed.", code=ProblemCode.INVALID_STATE_TRANSITION
        )
    if case.version != expected_version:
        raise DelphiError(
            "The case changed since it was loaded; reload it and decide again.",
            code=ProblemCode.VERSION_CONFLICT,
        )
    version = get_version(session, case.kb_version_id)
    if version is None:
        raise NotFoundError(f"No KB version {case.kb_version_id}.")
    profile = case_profile(version.kb, case.type.value)

    decision = disposition.decision
    if decision not in profile.decisions:
        raise _invalid(
            f"A {case.type.value} case is decided with one of {list(profile.decisions)}."
        )
    if disposition.confidence not in CONFIDENCE_RANGE:
        raise _invalid("Confidence is 1 to 5.")
    fraud_typologies = [h for h in profile.hypotheses if h not in (GENUINE, UNEXPLAINED)]
    fraud_typologies.append(FRAUD_OTHER)
    typology = disposition.typology
    if decision == "CONFIRM_FRAUD":
        if typology not in fraud_typologies:
            raise _invalid(f"CONFIRM_FRAUD names one of {fraud_typologies}.")
    elif typology is not None:
        raise _invalid("Only CONFIRM_FRAUD names a typology.")

    belief = belief_vector(profile, case.posterior)
    vulnerable = vulnerable_payer(session, case)
    losses = loss_matrix(profile, float(case.amount_at_stake_paise), vulnerable)
    recommended = profile.decisions[decide(belief, losses)[0]]
    risks = {
        d: float(r)
        for d, r in zip(profile.decisions, risk_by_decision(belief, losses), strict=True)
    }
    is_override = decision != recommended
    rationale = (disposition.override_rationale or "").strip()
    if is_override and len(rationale) < MIN_OVERRIDE_RATIONALE:
        raise _invalid(
            f"This overrides the recommendation ({recommended}): give a rationale of at least "
            f"{MIN_OVERRIDE_RATIONALE} characters."
        )

    now = clock.now()
    payment_status = None
    if case.type is CaseType.PAYMENT_HOLD and case.subject_payment_id is not None:
        payment_status = bank.apply_case_disposition(
            session, case.subject_payment_id, decision, now
        ).value
    _record_label(session, case, decision, typology, now)
    updated = session.execute(
        update(cases)
        .where(cases.c.id == case_id, cases.c.version == expected_version)
        .values(
            status=CaseStatus.DISPOSED.value,
            disposition=decision,
            disposition_typology=typology,
            disposition_confidence=disposition.confidence,
            is_override=is_override,
            override_rationale=rationale or None,
            disposed_by=user_id,
            disposed_at=now,
            recommended_decision=recommended,
            risk_by_decision=risks,
            version=expected_version + 1,
        )
        .returning(cases.c.id)
    ).scalar_one_or_none()
    if updated is None:  # the row lock makes this unreachable; never write past it
        raise DelphiError(
            "The case changed underneath this disposition.",
            code=ProblemCode.VERSION_CONFLICT,
        )
    payload: dict[str, Any] = {
        "decision": decision,
        "typology": typology,
        "confidence": disposition.confidence,
        "recommended_decision": recommended,
        "is_override": is_override,
        "payment_status": payment_status,
        "note": disposition.note,
    }
    record(
        session,
        clock,
        actor_type=ActorType.USER,
        actor_id=str(user_id),
        action="case.disposed",
        entity_type="case",
        entity_id=str(case_id),
        payload=payload,
    )
    publish(
        session,
        EventType.CASE_UPDATED,
        str(case_id),
        {"case_id": str(case_id), "status": CaseStatus.DISPOSED.value, "decision": decision},
    )
    disposed = get_case(session, case_id)
    assert disposed is not None
    return disposed


def _record_label(
    session: Session, case: CaseRecord, decision: str, typology: str | None, now: Any
) -> None:
    """docs/14 §7: an investigator's disposition is a label the bank now knows."""
    if case.type is CaseType.PAYMENT_HOLD and case.subject_payment_id is not None:
        subject, subject_type = case.subject_payment_id, "PAYMENT"
        key = typology if decision == "CONFIRM_FRAUD" and typology else GENUINE
    elif case.subject_account_id is not None:
        subject, subject_type = case.subject_account_id, "ACCOUNT"
        key = MULE if decision == "CONFIRM_MULE" else GENUINE
    else:
        return
    record_labels(
        session.connection(),
        [LabelRow(subject_type, subject, key, INVESTIGATOR_DISPOSITION, now)],
        now=now,
    )
