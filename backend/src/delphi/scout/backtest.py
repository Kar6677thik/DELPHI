"""``RUN_BACKTEST``: replay recent sessions and cases under ACTIVE + a draft (docs/11 §4, #118).

**The replay.** Every Guardian session decided in the last ``scout.lookback_days``, and every
case opened in them, that stored its prior components (``p_model`` and the anomaly percentile)
is replayed twice, under ACTIVE and under the candidate KB (``candidate_kb``): the prior is
rebuilt (docs/06 §11) and the stored observations are applied in their original order (a
session's completed probe runs; a case's current evidence records, after its session's probe
runs for a payment case), with the live Guardian's own steps (``observe``,
``final_decision``). Comparing the two replays isolates the draft's effect: both start from the
same stored inputs. An observation whose source or outcome the profile doesn't have is skipped.

A replay cannot run probes that were never run: an item keeps the observations it has. The
report says so (``limitations``).

**The report** (docs/11 §4): ``attributed`` (queue items whose argmax is now the draft's key),
``attributed_labelled_precision`` (among those with a label, the share labelled as fraud),
``genuine_disturbed`` (sessions labelled GENUINE whose decision changed), ``decision_changes``
(old → new counts) and ``unexplained_rate`` (before and after), plus how many sessions and cases
were replayed. Labels are the bank's observed labels (``delphi.labels``), never ground truth.
There is no pass/fail: the FDE engineer judges.

**Bounded** (after #263): at most ``max_items`` sessions and as many cases, the newest first,
in one transaction with ``SET LOCAL statement_timeout``/``work_mem``. Everything is loaded in a
fixed number of statements per chunk of ``ID_CHUNK`` ids (``= ANY(:ids)``), never per item, and
one backtest replays at a time (advisory lock 57): a busy lock raises ``BacktestBusyError``, so
the job is retried later rather than skipped.

**Labels** follow docs/05 §8's precedence (INVESTIGATOR_DISPOSITION first), the one the sensing
trainers use, not the latest known. **The queue** is the unexplained items not dismissed from the
Scout's queue.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import any_, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session, sessionmaker

from delphi.engine import CompiledProfile, build_prior, compile_profile
from delphi.guardian.api import (
    decided_sessions_with_prior,
    final_decision,
    observe,
    outcomes_by_session,
)
from delphi.kb.model import GENUINE, UNEXPLAINED, KnowledgeBase
from delphi.kb.store import active_version
from delphi.navigator.api import case_profile, cases_with_prior, current_outcomes_by_case
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError
from delphi.platform.jobs import JobContext
from delphi.platform.labels import labels
from delphi.platform.locks import BACKTEST_LOCK_KEY
from delphi.scout.drafts import (
    DraftInvalidError,
    backtest_target,
    candidate_kb,
    record_backtest,
    stored_spec,
)
from delphi.scout.queue import SubjectType
from delphi.scout.tables import scout_queue_dismissals
from delphi.sensing.api import label_precedence
from delphi.sim import api as bank

GUARDIAN_PROFILE = "GUARDIAN_PAYMENT"
PAYMENT = "PAYMENT"
ACCOUNT = "ACCOUNT"
STATEMENT_TIMEOUT = "30s"  # per statement, as in #263
WORK_MEM = "16MB"
ID_CHUNK = 1_000  # ids per `= ANY(:ids)` statement
LIMITATION = (
    "A replay cannot run probes that were never run: each item keeps only the observations it "
    "has, so a draft that would have asked a different question is not credited for its answer."
)


class BacktestBusyError(RuntimeError):
    """Another backtest holds lock 57. Raised, not skipped, so the job is retried: a skipped run
    would end its revision's job as if it had run."""


class UnfitPriorError(ValueError):
    """A stored prior that fits neither profile (``build_prior`` refuses it): the item is
    skipped. Any other error in a replay is a bug and is raised."""


@dataclass(frozen=True, slots=True)
class Item:
    """One stored session or case, as the replay needs it."""

    kind: str  # "session" or "case"
    profile_key: str  # GUARDIAN_PAYMENT, or the case type
    p_model: Mapping[str, float]
    anomaly_percentile: float
    observations: Sequence[tuple[str, str]]  # (source, outcome), in their original order
    amount_paise: int
    vulnerable: bool
    in_queue: bool  # flagged unexplained, and not dismissed from the Scout's queue
    label_subject: tuple[str, UUID] | None  # (subject type, id) of its label, if any


@dataclass(frozen=True, slots=True)
class Replayed:
    decision: str
    argmax: str


def replay(
    profile: CompiledProfile,
    p_model: Mapping[str, float],
    anomaly_percentile: float,
    observations: Iterable[tuple[str, str]],
    *,
    amount_paise: int,
    vulnerable: bool,
) -> Replayed:
    """The prior rebuilt (docs/06 §11), the observations applied in order, the decision."""
    try:
        belief = build_prior(profile, p_model, anomaly_percentile)
    except ValueError as err:
        raise UnfitPriorError(str(err)) from err
    for source, outcome in observations:
        src = profile.sources.get(source)
        if src is None or outcome not in src.outcomes:
            continue
        belief = observe(profile, belief, source, outcome)
    decision, _risks = final_decision(
        profile, belief, amount_paise=amount_paise, vulnerable=vulnerable
    )
    return Replayed(decision, profile.hypotheses[int(np.argmax(belief))])


def _bound(session: Session) -> None:
    session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    session.execute(text(f"SET LOCAL work_mem = '{WORK_MEM}'"))


def _chunks(ids: Iterable[UUID]) -> list[list[UUID]]:
    unique = list(dict.fromkeys(ids))
    return [unique[i : i + ID_CHUNK] for i in range(0, len(unique), ID_CHUNK)]


def _any(ids: Sequence[UUID]) -> Any:
    return any_(bindparam("ids", list(ids), type_=ARRAY(PG_UUID(as_uuid=True))))


def _payments(session: Session, payment_ids: Iterable[UUID]) -> dict[UUID, tuple[int, bool]]:
    """Each payment's amount and whether its payer is vulnerable, a chunk at a time."""
    p, a, c = bank.payments.c, bank.accounts.c, bank.customers.c
    found: dict[UUID, tuple[int, bool]] = {}
    for chunk in _chunks(payment_ids):
        rows = session.execute(
            select(p.id, p.amount_paise, c.is_vulnerable)
            .join(bank.accounts, a.id == p.payer_account_id)
            .outerjoin(bank.customers, c.id == a.customer_id)
            .where(p.id == _any(chunk))
        )
        found.update({r.id: (int(r.amount_paise), bool(r.is_vulnerable)) for r in rows})
    return found


def _dismissed(session: Session, subject_type: SubjectType, ids: Iterable[UUID]) -> set[UUID]:
    """The items dismissed from the Scout's queue (docs/11 §1): no longer in it."""
    d = scout_queue_dismissals.c
    found: set[UUID] = set()
    for chunk in _chunks(ids):
        found.update(
            session.execute(
                select(d.subject_id).where(
                    d.subject_type == subject_type.value, d.subject_id == _any(chunk)
                )
            ).scalars()
        )
    return found


def load_items(session: Session, clock: Clock, *, days: int, max_items: int) -> list[Item]:
    """The newest ``max_items`` decided sessions and ``max_items`` cases of the last ``days``
    that stored their prior components, with their observations, in a few statements per
    chunk of ids."""
    since = clock.now() - timedelta(days=days)
    decided = decided_sessions_with_prior(session, since=since, limit=max_items)
    case_rows = cases_with_prior(session, since=since, limit=max_items)
    outcomes = outcomes_by_session(
        session,
        [s.id for s in decided]
        + [c.guardian_session_id for c in case_rows if c.guardian_session_id is not None],
    )
    evidence = current_outcomes_by_case(session, [c.id for c in case_rows])
    payments = _payments(
        session,
        [s.payment_id for s in decided]
        + [c.subject_payment_id for c in case_rows if c.subject_payment_id is not None],
    )
    dismissed_sessions = _dismissed(
        session, SubjectType.GUARDIAN_SESSION, (s.id for s in decided if s.unexplained)
    )
    dismissed_cases = _dismissed(
        session, SubjectType.CASE, (c.id for c in case_rows if c.unexplained)
    )
    items: list[Item] = []
    for s in decided:
        amount, vulnerable = payments.get(s.payment_id, (0, False))
        items.append(
            Item(
                "session",
                GUARDIAN_PROFILE,
                s.p_model,
                s.anomaly_percentile,
                outcomes.get(s.id, []),
                amount,
                vulnerable,
                s.unexplained and s.id not in dismissed_sessions,
                (PAYMENT, s.payment_id),
            )
        )
    for c in case_rows:
        before = outcomes.get(c.guardian_session_id, []) if c.guardian_session_id else []
        payment = c.subject_payment_id
        subject = (
            (PAYMENT, payment)
            if payment is not None
            else ((ACCOUNT, c.subject_account_id) if c.subject_account_id else None)
        )
        items.append(
            Item(
                "case",
                c.type,
                c.p_model,
                c.anomaly_percentile,
                [*before, *evidence.get(c.id, [])],
                c.amount_at_stake_paise,
                payments.get(payment, (0, False))[1] if payment is not None else False,
                c.unexplained and c.id not in dismissed_cases,
                subject,
            )
        )
    return items


def _labels(session: Session, subjects: Iterable[tuple[str, UUID]]) -> dict[tuple[str, UUID], str]:
    """Each subject's label by docs/05 §8's precedence (an investigator's disposition over an
    outreach over a complaint over a listing over the 60-day no-complaint), whatever order they
    became known in. Filtered by subject type too, so ``(subject_type, subject_id, …)`` serves."""
    rank = {source: i for i, source in enumerate(label_precedence())}
    lb = labels.c
    by_type: dict[str, list[UUID]] = {}
    for subject_type, subject_id in subjects:
        by_type.setdefault(subject_type, []).append(subject_id)
    best: dict[tuple[str, UUID], tuple[int, str]] = {}
    for subject_type, ids in by_type.items():
        for chunk in _chunks(ids):
            rows = session.execute(
                select(lb.subject_id, lb.hypothesis_key, lb.label_source).where(
                    lb.subject_type == subject_type, lb.subject_id == _any(chunk)
                )
            )
            for r in rows:
                key = (subject_type, r.subject_id)
                score = rank.get(r.label_source, len(rank))
                if key not in best or score < best[key][0]:
                    best[key] = (score, r.hypothesis_key)
    return {key: hypothesis for key, (_score, hypothesis) in best.items()}


def report(
    active: KnowledgeBase,
    candidate: KnowledgeBase,
    new_key: str,
    items: Sequence[Item],
    item_labels: Mapping[tuple[str, UUID], str],
) -> dict[str, Any]:
    """docs/11 §4's report over ``items``, replayed under ``active`` and ``candidate``."""
    profiles: dict[tuple[str, str], CompiledProfile] = {}

    def profile(which: str, key: str) -> CompiledProfile:
        kb = active if which == "active" else candidate
        if (which, key) not in profiles:
            profiles[(which, key)] = (
                compile_profile(kb, key) if key == GUARDIAN_PROFILE else case_profile(kb, key)
            )
        return profiles[(which, key)]

    changes: Counter[str] = Counter()
    attributed = attributed_labelled = attributed_fraud = genuine_disturbed = 0
    unexplained_before = unexplained_after = 0
    skipped = 0
    for item in items:
        try:
            old, new = (
                replay(
                    profile(which, item.profile_key),
                    item.p_model,
                    item.anomaly_percentile,
                    item.observations,
                    amount_paise=item.amount_paise,
                    vulnerable=item.vulnerable,
                )
                for which in ("active", "candidate")
            )
        except UnfitPriorError:  # its stored prior fits neither profile
            skipped += 1
            continue
        changes[f"{old.decision}→{new.decision}"] += 1
        unexplained_before += old.argmax == UNEXPLAINED
        unexplained_after += new.argmax == UNEXPLAINED
        label = item_labels.get(item.label_subject) if item.label_subject else None
        if item.in_queue and new.argmax == new_key:
            attributed += 1
            if label is not None:
                attributed_labelled += 1
                attributed_fraud += label != GENUINE
        if item.kind == "session" and label == GENUINE and old.decision != new.decision:
            genuine_disturbed += 1
    n = len(items) - skipped
    return {
        "new_hypothesis": new_key,
        "sessions": sum(i.kind == "session" for i in items),
        "cases": sum(i.kind == "case" for i in items),
        "skipped": skipped,
        "attributed": attributed,
        "attributed_labelled": attributed_labelled,
        "attributed_labelled_precision": (
            attributed_fraud / attributed_labelled if attributed_labelled else None
        ),
        "genuine_disturbed": genuine_disturbed,
        "decision_changes": dict(sorted(changes.items())),
        "unexplained_rate": {
            "before": unexplained_before / n if n else None,
            "after": unexplained_after / n if n else None,
        },
        "limitations": [LIMITATION],
    }


def backtest(
    session: Session,
    clock: Clock,
    active: KnowledgeBase,
    candidate: KnowledgeBase,
    new_key: str,
    *,
    max_items: int,
) -> dict[str, Any]:
    """Load, replay and report, in the caller's transaction (bounded)."""
    _bound(session)
    items = load_items(session, clock, days=active.config.scout.lookback_days, max_items=max_items)
    item_labels = _labels(session, (i.label_subject for i in items if i.label_subject))
    return report(active, candidate, new_key, items, item_labels)


@dataclass(frozen=True, slots=True)
class RunBacktestJob:
    """``RUN_BACKTEST`` (``POST /typology-drafts/{id}/backtest``, docs/04 §5.4): params
    ``draft_id`` and ``revision``. A draft edited since (another revision), or no longer
    backtestable, is skipped and nothing is written."""

    sessions: sessionmaker[Session]
    clock: Clock
    max_items: int

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        draft_id = UUID(str(ctx.job.params["draft_id"]))
        revision = int(ctx.job.params["revision"])
        with self.sessions.begin() as session:
            version = active_version(session)
            if version is None:
                return {"skipped": "no ACTIVE KB version"}
            try:
                row = backtest_target(session, version.kb, draft_id)
            except (DelphiError, DraftInvalidError) as err:
                return {"skipped": str(err)}
            if row.revision != revision:
                return {"skipped": f"the draft is at revision {row.revision} now"}
            # After the cheap checks: an older revision's job skips without waiting for the lock.
            if not session.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": BACKTEST_LOCK_KEY}
            ).scalar_one():
                raise BacktestBusyError("another backtest is replaying; this job is retried")
            spec = stored_spec(row)
            candidate, _issues = candidate_kb(version.kb, spec)
            assert candidate is not None and spec.key is not None  # backtest_target validated it
            result = backtest(
                session, self.clock, version.kb, candidate, spec.key, max_items=self.max_items
            )
            result = {**result, "revision": revision, "kb_version_id": version.id}
            written = record_backtest(
                session, self.clock, draft_id, revision=revision, report=result
            )
        return {"draft_id": str(draft_id), "revision": revision, "written": written}
