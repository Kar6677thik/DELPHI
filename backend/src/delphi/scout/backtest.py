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
in one transaction with ``SET LOCAL statement_timeout``/``work_mem``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from delphi.engine import CompiledProfile, build_prior, compile_profile
from delphi.guardian.api import final_decision, observe, observed_outcomes
from delphi.guardian.tables import guardian_sessions
from delphi.kb.model import GENUINE, UNEXPLAINED, KnowledgeBase
from delphi.kb.store import active_version
from delphi.navigator.api import case_profile, current_records
from delphi.navigator.tables import cases
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError
from delphi.platform.jobs import JobContext
from delphi.platform.labels import labels
from delphi.scout.drafts import (
    DraftInvalidError,
    backtest_target,
    candidate_kb,
    record_backtest,
    stored_spec,
)
from delphi.sim import api as bank

GUARDIAN_PROFILE = "GUARDIAN_PAYMENT"
PAYMENT = "PAYMENT"
ACCOUNT = "ACCOUNT"
STATEMENT_TIMEOUT = "30s"  # per statement, as in #263
WORK_MEM = "16MB"
LIMITATION = (
    "A replay cannot run probes that were never run: each item keeps only the observations it "
    "has, so a draft that would have asked a different question is not credited for its answer."
)


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
    in_queue: bool  # flagged unexplained
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
    belief = build_prior(profile, p_model, anomaly_percentile)
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


def _vulnerable_by_payment(session: Session, payment_ids: Sequence[UUID]) -> dict[UUID, bool]:
    p, a, c = bank.payments.c, bank.accounts.c, bank.customers.c
    if not payment_ids:
        return {}
    rows = session.execute(
        select(p.id, p.amount_paise, c.is_vulnerable)
        .join(bank.accounts, a.id == p.payer_account_id)
        .outerjoin(bank.customers, c.id == a.customer_id)
        .where(p.id.in_(list(payment_ids)))
    )
    return {r.id: bool(r.is_vulnerable) for r in rows}


def load_items(session: Session, clock: Clock, *, days: int, max_items: int) -> list[Item]:
    """The newest ``max_items`` decided sessions and ``max_items`` cases of the last ``days``
    that stored their prior components."""
    since = clock.now() - timedelta(days=days)
    g, k, p = guardian_sessions.c, cases.c, bank.payments.c
    sessions = list(
        session.execute(
            select(
                g.id, g.payment_id, g.p_model, g.anomaly_percentile, g.unexplained, p.amount_paise
            )
            .join(bank.payments, p.id == g.payment_id)
            .where(
                g.decided_at >= since,
                g.p_model.is_not(None),
                g.anomaly_percentile.is_not(None),
            )
            .order_by(g.decided_at.desc(), g.id.desc())
            .limit(max_items)
        )
    )
    case_rows = list(
        session.execute(
            select(
                k.id,
                k.type,
                k.subject_payment_id,
                k.subject_account_id,
                k.guardian_session_id,
                k.prior_components,
                k.unexplained,
                k.amount_at_stake_paise,
            )
            .where(k.opened_at >= since)
            .order_by(k.opened_at.desc(), k.id.desc())
            .limit(max_items)
        )
    )
    vulnerable = _vulnerable_by_payment(
        session,
        [r.payment_id for r in sessions]
        + [r.subject_payment_id for r in case_rows if r.subject_payment_id is not None],
    )
    items: list[Item] = []
    session_outcomes: dict[UUID, list[tuple[str, str]]] = {}
    for r in sessions:
        outcomes = list(observed_outcomes(session, r.id).items())
        session_outcomes[r.id] = outcomes
        items.append(
            Item(
                "session",
                GUARDIAN_PROFILE,
                dict(r.p_model),
                float(r.anomaly_percentile),
                outcomes,
                int(r.amount_paise),
                vulnerable.get(r.payment_id, False),
                bool(r.unexplained),
                (PAYMENT, r.payment_id),
            )
        )
    for c in case_rows:
        stored = c.prior_components or {}
        p_model, q = stored.get("p_model"), stored.get("anomaly_percentile")
        if not p_model or q is None:
            continue
        before: list[tuple[str, str]] = []
        if c.guardian_session_id is not None:
            before = session_outcomes.get(c.guardian_session_id) or list(
                observed_outcomes(session, c.guardian_session_id).items()
            )
        evidence = [(e.source_key, e.outcome) for e in current_records(session, c.id) if e.outcome]
        payment = c.subject_payment_id
        items.append(
            Item(
                "case",
                c.type,
                {str(h): float(v) for h, v in p_model.items()},
                float(q),
                [*before, *evidence],
                int(c.amount_at_stake_paise),
                vulnerable.get(payment, False) if payment is not None else False,
                bool(c.unexplained),
                (PAYMENT, payment)
                if payment is not None
                else ((ACCOUNT, c.subject_account_id) if c.subject_account_id else None),
            )
        )
    return items


def _labels(session: Session, subjects: Iterable[tuple[str, UUID]]) -> dict[tuple[str, UUID], str]:
    wanted = list({s for s in subjects})
    if not wanted:
        return {}
    lb = labels.c
    rows = session.execute(
        select(lb.subject_type, lb.subject_id, lb.hypothesis_key, lb.known_at)
        .where(lb.subject_id.in_([i for _, i in wanted]))
        .order_by(lb.known_at)
    )
    found: dict[tuple[str, UUID], str] = {}
    for r in rows:
        found[(r.subject_type, r.subject_id)] = r.hypothesis_key  # the latest known label wins
    return found


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
        except ValueError:  # its stored prior fits neither profile (build_prior refuses it)
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
