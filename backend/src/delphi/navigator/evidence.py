"""The evidence fetch flow (docs/09 §3, FR-NAV-05): one evidence request against a case.

``request_evidence`` runs in the caller's transaction:

1. it locks the case (one writer at a time) and refuses a disposed one;
2. it returns the current record unchanged for a source already fetched (idempotent per case and
   source) unless ``refresh``, which supersedes it;
3. it ranks the source as the console would at that moment (rank and net value at request);
4. it runs the source's adapter over the bank and classifies the facts (docs/09 §4), or takes the
   investigator's recorded outcome for ``CUSTOMER_OUTREACH_CALL``;
5. it updates the case posterior with the KB likelihoods (engine.update), stores the record, and
   writes the audit entry and the `case.updated` event. An outreach call that confirms coercion
   or a denial also records an `OUTREACH_CONFIRMED` label (docs/09 §3).

A refresh replaces an observation, it does not add one: the superseded record's likelihood is
divided out of the case posterior before the new outcome's is applied, so the same outcome again
leaves the posterior where it was. A refreshed outreach call replaces its label too.

The narrative job (docs/15) and the simulated outreach call come next (#102 part 3).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from delphi.engine.belief import kl_bits
from delphi.engine.belief import update as bayes_update
from delphi.engine.types import CompiledProfile
from delphi.kb.model import UNEXPLAINED, KnowledgeBase
from delphi.kb.store import get_version
from delphi.navigator.console import belief_vector, case_profile, console, recommend
from delphi.navigator.disposition import vulnerable_payer
from delphi.navigator.evidence_adapters import ADAPTERS, EvidenceSubject
from delphi.navigator.store import CaseRecord, CaseStatus, get_case
from delphi.navigator.tables import cases, evidence_records
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.platform.jobs import JobStatus, JobType, enqueue
from delphi.platform.labels import LabelRow, replace_label
from delphi.platform.tables import jobs
from delphi.probes.api import EVIDENCE_CLASSIFIERS, RECORDED_EVIDENCE
from delphi.sim import api as bank

OUTREACH = "CUSTOMER_OUTREACH_CALL"
OUTREACH_LABEL = "OUTREACH_CONFIRMED"
APP_PREFIX = "APP_"
ATO = "ATO"
_MS = 1000


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    id: UUID
    case_id: UUID
    source_key: str
    outcome: str
    raw: Mapping[str, Any]
    record_refs: Sequence[str]
    fetch_ms: int
    posterior_before: Mapping[str, float]
    posterior_after: Mapping[str, float]
    info_yield_bits: float
    rank_at_request: int | None
    net_value_at_request_paise: int | None
    requested_by: UUID | None
    requested_at: datetime
    narrative: str | None
    narrative_source: str | None

    @staticmethod
    def of(row: Any) -> EvidenceRecord:
        return EvidenceRecord(
            id=row.id,
            case_id=row.case_id,
            source_key=row.source_key,
            outcome=row.outcome,
            raw=row.raw,
            record_refs=row.record_refs,
            fetch_ms=row.fetch_ms,
            posterior_before=row.posterior_before,
            posterior_after=row.posterior_after,
            info_yield_bits=row.info_yield_bits,
            rank_at_request=row.rank_at_request,
            net_value_at_request_paise=row.net_value_at_request_paise,
            requested_by=row.requested_by,
            requested_at=row.requested_at,
            narrative=row.narrative,
            narrative_source=row.narrative_source,
        )


def current_records(session: Session, case_id: UUID) -> list[EvidenceRecord]:
    """The case's current (not superseded) records, in the order they were fetched."""
    e = evidence_records.c
    rows = session.execute(
        select(evidence_records)
        .where(e.case_id == case_id, e.superseded_by.is_(None))
        .order_by(e.requested_at, e.id)
    )
    return [EvidenceRecord.of(r) for r in rows]


def observed_sources(session: Session, case_id: UUID) -> frozenset[str]:
    return frozenset(r.source_key for r in current_records(session, case_id))


def _invalid(message: str) -> DelphiError:
    return DelphiError(message, code=ProblemCode.VALIDATION_FAILED)


def _subject(session: Session, case: CaseRecord, now: datetime) -> EvidenceSubject:
    """docs/09 §4: windows run to the held payment's initiation, or the review's opening."""
    if case.subject_payment_id is not None:
        payment = bank.get_payment(session, case.subject_payment_id)
        if payment is None:
            raise NotFoundError(f"No payment {case.subject_payment_id}.")
        return EvidenceSubject(
            as_of=payment.initiated_at,
            now=now,
            counterparty_account_id=payment.payee_account_id,
            payer_account_id=payment.payer_account_id,
            payer_customer_id=payment.payer_customer_id,
        )
    owner = session.execute(
        select(bank.accounts.c.customer_id).where(bank.accounts.c.id == case.subject_account_id)
    ).scalar_one_or_none()
    return EvidenceSubject(
        as_of=case.opened_at,
        now=now,
        counterparty_account_id=case.subject_account_id,
        payer_account_id=case.subject_account_id,
        payer_customer_id=owner,
    )


def narrative_subject(
    session: Session, case: CaseRecord, subject: str | None
) -> tuple[UUID | None, datetime]:
    """The account a narrative is about (the KB source's ``subject``: the counterparty, or the
    payer for PAYER and PAYER_TO_COUNTERPARTY sources) and the case's ``as_of``."""
    s = _subject(session, case, case.opened_at)
    account = s.counterparty_account_id if subject == "COUNTERPARTY" else s.payer_account_id
    return account, s.as_of


def _leading(posterior: Mapping[str, float], order: Sequence[str]) -> str:
    return max(order, key=lambda h: (posterior.get(h, 0.0), -order.index(h)))


def _outreach_label(outcome: str, posterior: Mapping[str, float]) -> str | None:
    """docs/09 §3: coercion → the leading APP_* hypothesis; a denial → ATO."""
    if outcome == "DENIES_INITIATING":
        return ATO
    if outcome == "CONFIRMS_COERCION":
        apps = [h for h in posterior if h.startswith(APP_PREFIX)]
        return max(apps, key=lambda h: posterior[h]) if apps else None
    return None


def _rank(
    session: Session,
    case: CaseRecord,
    *,
    kb: KnowledgeBase,
    profile: CompiledProfile,
    posterior: Mapping[str, float],
    observed: frozenset[str],
    source_key: str,
) -> tuple[int | None, float | None]:
    """The source's place and net value in the console's ranking at this moment (docs/09 §2),
    from ``posterior``: the case's, or for a refresh the case's without the superseded
    observation."""
    view = console(
        kb,
        profile,
        posterior=posterior,
        amount_paise=case.amount_at_stake_paise,
        vulnerable=vulnerable_payer(session, case),
        protocol=case.protocol.value,
        observed=observed,
    )
    ranked = [r for r in view.next_best_evidence if r.eligible]
    row = next((i for i, r in enumerate(ranked) if r.source_key == source_key), None)
    return (None, None) if row is None else (row + 1, ranked[row].net_value_paise)


def _fetch(
    session: Session,
    case: CaseRecord,
    *,
    kb: KnowledgeBase,
    source_key: str,
    now: datetime,
    recorded_outcome: str | None,
    note: str | None,
) -> tuple[str, dict[str, Any], list[str], int]:
    """The outcome, the facts, the record ids and how long the fetch took (ms)."""
    started = time.perf_counter()
    if source_key in RECORDED_EVIDENCE:
        if recorded_outcome not in RECORDED_EVIDENCE[source_key]:
            raise _invalid(
                f"{source_key} needs the recorded outcome, one of "
                f"{sorted(RECORDED_EVIDENCE[source_key])}."
            )
        # docs/15 §7: the outreach narrative's facts are {result, note}.
        return recorded_outcome, {"result": recorded_outcome, "note": note}, [], 0
    thresholds = kb.config.classifiers
    fetched = ADAPTERS[source_key](session, _subject(session, case, now), thresholds)
    outcome = EVIDENCE_CLASSIFIERS[source_key].classify(fetched.observation, thresholds)
    fetch_ms = round((time.perf_counter() - started) * _MS)
    return outcome, dict(fetched.facts), list(fetched.record_refs), fetch_ms


def request_evidence(
    session: Session,
    clock: Clock,
    case_id: UUID,
    source_key: str,
    *,
    requested_by: UUID | None,
    refresh: bool = False,
    recorded_outcome: str | None = None,
    note: str | None = None,
    on_open: bool = False,
) -> EvidenceRecord:
    """Fetch (or record) one evidence item and update the case's posterior (docs/09 §3).

    ``on_open``: the record a trigger takes as the case opens (docs/09 §1, the complaints
    trigger's LINKED_COMPLAINTS); it is no investigator action, so the case stays OPEN.
    """
    locked = session.execute(
        select(cases.c.id).where(cases.c.id == case_id).with_for_update()
    ).scalar_one_or_none()
    case = get_case(session, case_id) if locked else None
    if case is None:
        raise NotFoundError("No case with this id.")
    if case.status is CaseStatus.DISPOSED:
        raise DelphiError(
            "A disposed case takes no more evidence.", code=ProblemCode.INVALID_STATE_TRANSITION
        )
    version = get_version(session, case.kb_version_id)
    if version is None:
        raise NotFoundError(f"No KB version {case.kb_version_id}.")
    profile = case_profile(version.kb, case.type.value)
    source = profile.sources.get(source_key)
    if source is None:
        raise _invalid(f"{source_key} is not an evidence source for a {case.type.value} case.")
    if not source.enabled:  # docs/06 §8: a disabled source is never eligible
        raise _invalid(f"{source_key} is disabled in KB version {case.kb_version_id}.")
    if source_key not in RECORDED_EVIDENCE and (recorded_outcome is not None or note is not None):
        raise _invalid(f"{source_key} takes no recorded outcome or note; its rule decides.")
    current = {r.source_key: r for r in current_records(session, case_id)}
    if source_key in current and not refresh:
        return current[source_key]  # docs/09 §3: idempotent per (case, source)

    before = belief_vector(profile, case.posterior)
    if source_key in current:  # a refresh: take the superseded observation back out first
        old = source.likelihood[source.outcomes.index(current[source_key].outcome)]
        before = bayes_update(before, 1.0 / old)  # the KB floors likelihoods, so never 0
    names = profile.hypotheses
    posterior_before = {h: float(p) for h, p in zip(names, before, strict=True)}
    rank, net_value = _rank(
        session,
        case,
        kb=version.kb,
        profile=profile,
        posterior=posterior_before,  # a refresh ranks without the observation it replaces
        observed=frozenset(current) - {source_key},
        source_key=source_key,
    )
    now = clock.now()
    outcome, facts, refs, fetch_ms = _fetch(
        session,
        case,
        kb=version.kb,
        source_key=source_key,
        now=now,
        recorded_outcome=recorded_outcome,
        note=note,
    )
    after = bayes_update(before, source.likelihood[source.outcomes.index(outcome)])
    posterior_after = {h: float(p) for h, p in zip(names, after, strict=True)}
    record_id = uuid7(int(now.timestamp() * _MS))
    values = {
        "id": record_id,
        "case_id": case_id,
        "source_key": source_key,
        "outcome": outcome,
        "raw": facts,
        "record_refs": list(refs),
        "fetch_ms": fetch_ms,
        "posterior_before": posterior_before,
        "posterior_after": posterior_after,
        "info_yield_bits": kl_bits(after, before),
        "rank_at_request": rank,
        "net_value_at_request_paise": None if net_value is None else round(net_value),
        "requested_by": requested_by,
        "requested_at": now,
    }
    if source_key in current:  # a refresh supersedes the current record
        session.execute(
            update(evidence_records)
            .where(evidence_records.c.id == current[source_key].id)
            .values(superseded_by=record_id)
        )
    session.execute(evidence_records.insert().values(**values))
    enqueue(  # docs/09 §3: the narrative is written by the worker (docs/15)
        session,
        clock,
        JobType.LLM_NARRATIVE,
        idempotency_key=f"narrative:{record_id}",
        params={"evidence_record_id": str(record_id)},
    )
    order = [h for h in names if h != UNEXPLAINED] + [UNEXPLAINED]
    advice = recommend(  # #208: the queue's recommendation follows the posterior
        profile,
        posterior_after,
        amount_paise=case.amount_at_stake_paise,
        vulnerable=vulnerable_payer(session, case),
    )
    session.execute(
        update(cases)
        .where(cases.c.id == case_id)
        .values(
            posterior=posterior_after,
            leading_hypothesis=_leading(posterior_after, [h for h in order if h in names]),
            recommended_decision=advice.decision,
            risk_by_decision=dict(advice.risk_by_decision),
            status=case.status.value if on_open else CaseStatus.IN_PROGRESS.value,
            version=case.version + 1,
        )
    )
    if source_key == OUTREACH and case.subject_payment_id is not None:
        hypothesis = _outreach_label(outcome, posterior_after)
        if hypothesis is not None or source_key in current:  # a refresh corrects the label
            replace_label(
                session.connection(),
                subject_type="PAYMENT",
                subject_id=case.subject_payment_id,
                label_source=OUTREACH_LABEL,
                row=None
                if hypothesis is None
                else LabelRow(
                    subject_type="PAYMENT",
                    subject_id=case.subject_payment_id,
                    hypothesis_key=hypothesis,
                    label_source=OUTREACH_LABEL,
                    known_at=now,
                ),
                now=now,
            )
    record(
        session,
        clock,
        actor_type=ActorType.USER if requested_by else ActorType.SYSTEM,
        actor_id=str(requested_by) if requested_by else None,
        action="evidence.recorded",
        entity_type="case",
        entity_id=str(case_id),
        payload={
            "evidence_record_id": str(record_id),
            "source_key": source_key,
            "outcome": outcome,
            "refresh": source_key in current,
        },
    )
    publish(
        session,
        EventType.CASE_UPDATED,
        str(case_id),
        {"case_id": str(case_id), "evidence_record_id": str(record_id), "source_key": source_key},
    )
    return EvidenceRecord.of(
        session.execute(select(evidence_records).where(evidence_records.c.id == record_id)).one()
    )


def request_simulated_outreach(
    session: Session,
    clock: Clock,
    case_id: UUID,
    *,
    requested_by: UUID | None,
    simulator: bool,
) -> bool:
    """docs/09 §3: in simulation the outreach call is the worker's (`SIM_OUTREACH_CALL`): it
    samples the customer's answer and records it through ``request_evidence``. Returns whether a
    call was queued: not when the case already has the call's record, nor while an earlier call
    is still queued or running. A call that ended without a record (no ground truth, a failure)
    can be placed again: each attempt has its own job key. ``simulator`` says whether this
    deployment runs the simulator's jobs (``DELPHI_SIMULATION_ENABLED``); without it the call
    could never run, so it is refused (503) rather than queued, once the case itself is known
    to be valid (404 and 409 come first)."""
    case = get_case(session, case_id)
    if case is None:
        raise NotFoundError("No case with this id.")
    if case.status is CaseStatus.DISPOSED:
        raise DelphiError(
            "A disposed case takes no more evidence.", code=ProblemCode.INVALID_STATE_TRANSITION
        )
    if case.subject_payment_id is None:
        raise _invalid("A simulated outreach call is about a held payment.")
    if not simulator:
        raise DelphiError(
            "This deployment does not run the simulator, so no simulated call can be placed.",
            code=ProblemCode.NOT_INITIALISED,
        )
    if OUTREACH in observed_sources(session, case_id):
        return False
    j = jobs.c
    attempts = session.execute(
        select(j.status).where(
            j.type == JobType.SIM_OUTREACH_CALL.value,
            j.params["case_id"].astext == str(case_id),
        )
    ).scalars()
    statuses = list(attempts)
    if any(s in (JobStatus.QUEUED.value, JobStatus.RUNNING.value) for s in statuses):
        return False  # the call is under way; its record arrives over SSE
    enqueue(
        session,
        clock,
        JobType.SIM_OUTREACH_CALL,
        idempotency_key=f"outreach:{case_id}:{len(statuses) + 1}",
        params={"case_id": str(case_id), "payment_id": str(case.subject_payment_id)},
        created_by=requested_by,
    )
    return True
