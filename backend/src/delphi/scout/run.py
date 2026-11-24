"""The ``RUN_SCOUT`` job and its auto-trigger (docs/11 §2, FR-SCT-02, #117).

A run takes the unexplained queue's items of the last ``scout.lookback_days`` and, as the
reference, the sessions of the same period attributed to a known typology. For each item it
loads what the Scout sees (``ScoutItem``):
- its observed outcomes: a session's completed probe runs, a case's current evidence records;
- the doc 13 §3 `scout` payment features, as of the payment's initiation (a case about an account
  has no payment, so its features sit at the mean);
- the payment channel.

It then runs the clustering core (``delphi.scout.clustering.scout``) with the KB's `scout:`
numbers, `cluster_selection_epsilon` included. It stores the run, one ``scout_clusters`` row per
cluster (members as ``{subject_type, subject_id}`` refs, its size and time span) and one
``typology_drafts`` row per cluster, status CANDIDATE, whose spec holds the suggested name, the
draft likelihood rows and the outcome profile.

**Bounds (after #263).** A run loads at most ``scout_max_items`` queue items (the newest of the
lookback) and as many reference sessions, with the sensing config parsed once. The reference
set's argmax is taken in SQL. A retried ``RUN_SCOUT`` (a lost lease) finds its run by ``job_id``
(unique) and returns it. ``SCOUT_AUTO_TRIGGER`` enqueues nothing while a ``RUN_SCOUT`` is QUEUED
or RUNNING, so slow or failing runs do not pile up.

**Gap fills (docs/11 §2 is silent):**
- The sensing artifacts store no feature means or standard deviations, so the z-scores use the
  run's own items (queue and reference).
- The F3 stage indicators are left empty: the spec names the four stages (A-07) but not how a
  customer's events map to them (asked on #117).

``SCOUT_AUTO_TRIGGER`` (every 5 min) enqueues a run once ``auto_trigger_new_items`` items have
entered the queue since the last run started, unless one is already QUEUED or RUNNING.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from delphi.guardian.api import attributed_sessions_query, observed_outcomes
from delphi.kb.model import KnowledgeBase
from delphi.kb.store import active_version
from delphi.navigator.api import current_records
from delphi.platform.clock import Clock
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.platform.jobs import JobContext, JobStatus, JobType, enqueue
from delphi.platform.tables import jobs
from delphi.scout.clustering import FeatureSpace, ScoutItem, scout
from delphi.scout.queue import QueueItem, SubjectType, count_queue_items, queue_items
from delphi.scout.tables import scout_clusters, scout_runs, typology_drafts
from delphi.sensing.api import FeatureConfig, scout_feature_config, scout_features
from delphi.sim import api as bank

CANDIDATE = "CANDIDATE"
CHANNELS = ("UPI_PUSH", "UPI_COLLECT_APPROVAL", "IMPS", "NEFT")  # customer channels (docs/05)


@dataclass(frozen=True, slots=True)
class _Loaded:
    ref: QueueItem
    item: ScoutItem


def _channel(session: Session, payment_id: UUID | None) -> str:
    if payment_id is None:
        return ""
    payment = bank.get_payment(session, payment_id)
    return payment.channel if payment is not None else ""


def _load(session: Session, item: QueueItem, config: FeatureConfig) -> ScoutItem:
    if item.subject_type is SubjectType.GUARDIAN_SESSION:
        outcomes = observed_outcomes(session, item.subject_id)
        features = (
            scout_features(session.connection(), item.payment_id, config)
            if item.payment_id
            else None
        )
    else:
        outcomes = {r.source_key: r.outcome for r in current_records(session, item.subject_id)}
        features = None
    return ScoutItem(
        id=str(item.subject_id),
        outcomes=outcomes,
        features=features or {},
        channel=_channel(session, item.payment_id),
    )


def _space(kb: KnowledgeBase, items: Sequence[ScoutItem]) -> FeatureSpace:
    sources = {k: tuple(s.outcomes) for k, s in kb.sources.items()}
    names = sorted({f for i in items for f in i.features})
    values = {n: np.asarray([i.features[n] for i in items if n in i.features]) for n in names}
    return FeatureSpace(
        sources=sources,
        feature_means={n: float(v.mean()) for n, v in values.items()},
        feature_stds={n: float(v.std()) for n, v in values.items()},
        channels=CHANNELS,
        f3_stages=(),
    )


def _reference(session: Session, *, since: datetime, limit: int) -> list[QueueItem]:
    """The newest ``limit`` sessions attributed to a known typology since ``since``."""
    ref = attributed_sessions_query(since).subquery("ref")
    rows = session.execute(
        select(ref).order_by(ref.c.entered_at.desc(), ref.c.subject_id.desc()).limit(limit)
    )
    return [
        QueueItem(SubjectType.GUARDIAN_SESSION, r.subject_id, r.entered_at, 0.0, r.payment_id)
        for r in rows
    ]


def _run_of_job(session: Session, job_id: UUID | None) -> UUID | None:
    if job_id is None:
        return None
    found: UUID | None = session.execute(
        select(scout_runs.c.id).where(scout_runs.c.job_id == job_id)
    ).scalar()
    return found


def run_scout(
    session: Session,
    clock: Clock,
    kb: KnowledgeBase,
    *,
    max_items: int,
    job_id: UUID | None = None,
) -> UUID:
    """One Scout run (docs/11 §2), in the caller's transaction. Returns the run's id; a job that
    already stored its run (a retry) gets that run back."""
    existing = _run_of_job(session, job_id)
    if existing is not None:
        return existing
    cfg = kb.config.scout
    started = clock.now()
    since = started - timedelta(days=cfg.lookback_days)
    config = scout_feature_config()
    newest = queue_items(session, kb, since=since, limit=max_items, newest_first=True)
    queue = [_Loaded(q, _load(session, q, config)) for q in reversed(newest)]
    reference = [
        _load(session, r, config) for r in _reference(session, since=since, limit=max_items)
    ]
    items = [q.item for q in queue]
    result = scout(
        items,
        reference,
        _space(kb, [*items, *reference]),
        min_cluster_size=cfg.min_cluster_size,
        min_samples=cfg.min_samples,
        max_ess=kb.config.elicitation.default_ess,
        selection_epsilon=cfg.cluster_selection_epsilon,
    )
    run_id = uuid7(unix_ms=int(started.timestamp() * 1000))
    params = {
        "lookback_days": cfg.lookback_days,
        "min_cluster_size": cfg.min_cluster_size,
        "min_samples": cfg.min_samples,
        "cluster_selection_epsilon": cfg.cluster_selection_epsilon,
        "n_reference": len(reference),
        "max_items": max_items,
    }
    session.execute(
        scout_runs.insert().values(
            id=run_id,
            job_id=job_id,
            started_at=started,
            finished_at=clock.now(),
            n_items=len(queue),
            n_clusters=len(result.clusters),
            params=params,
        )
    )
    by_id = {q.item.id: q.ref for q in queue}
    for n, cluster in enumerate(result.clusters, start=1):
        members = [by_id[m] for m in cluster.member_ids]
        cluster_id = uuid7(unix_ms=int(started.timestamp() * 1000) + n)
        times = [m.entered_at for m in members]
        session.execute(
            scout_clusters.insert().values(
                id=cluster_id,
                run_id=run_id,
                size=len(members),
                member_refs=[
                    {"subject_type": m.subject_type.value, "subject_id": str(m.subject_id)}
                    for m in members
                ],
                distinguishing_features={
                    "features": cluster.distinguishing_features,
                    "first_at": min(times).isoformat(),
                    "last_at": max(times).isoformat(),
                },
                outcome_profile=cluster.outcome_profile,
                created_at=started,
            )
        )
        draft_id = uuid7(unix_ms=int(started.timestamp() * 1000) + n)
        session.execute(
            typology_drafts.insert().values(
                id=draft_id,
                cluster_id=cluster_id,
                status=CANDIDATE,
                revision=1,
                spec={
                    "name": cluster.suggested_name,
                    "likelihoods": cluster.draft_likelihoods,
                    "outcome_profile": cluster.outcome_profile,
                },
                editors=[],
                created_at=started,
                updated_at=started,
                version=1,
            )
        )
        publish(
            session,
            EventType.SCOUT_CLUSTER_CREATED,
            str(cluster_id),
            {"run_id": str(run_id), "cluster_id": str(cluster_id), "size": len(members)},
        )
    return run_id


@dataclass(frozen=True, slots=True)
class RunScoutJob:
    """``RUN_SCOUT`` (docs/04 §5.4)."""

    sessions: sessionmaker[Session]
    clock: Clock
    max_items: int

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        with self.sessions.begin() as session:
            version = active_version(session)
            if version is None:
                return {"run_id": None, "reason": "no ACTIVE KB version"}
            run_id = run_scout(
                session, self.clock, version.kb, max_items=self.max_items, job_id=ctx.job.id
            )
        return {"run_id": str(run_id)}


def new_items_since_last_run(session: Session, kb: KnowledgeBase) -> int:
    last: datetime | None = session.execute(select(func.max(scout_runs.c.started_at))).scalar()
    return count_queue_items(session, kb, since=last)


def run_pending(session: Session) -> bool:
    """A RUN_SCOUT is QUEUED or RUNNING (a retry after a lost lease included)."""
    j = jobs.c
    return bool(
        session.execute(
            select(
                exists().where(
                    j.type == JobType.RUN_SCOUT.value,
                    j.status.in_((JobStatus.QUEUED.value, JobStatus.RUNNING.value)),
                )
            )
        ).scalar()
    )


@dataclass(frozen=True, slots=True)
class ScoutAutoTriggerJob:
    """``SCOUT_AUTO_TRIGGER`` (every 5 min): a run once enough new items are waiting, and none
    is pending."""

    sessions: sessionmaker[Session]
    clock: Clock

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        with self.sessions.begin() as session:
            version = active_version(session)
            if version is None:
                return {"enqueued": False}
            if run_pending(session):
                return {"enqueued": False, "reason": "a run is pending"}
            waiting = new_items_since_last_run(session, version.kb)
            if waiting < version.kb.config.scout.auto_trigger_new_items:
                return {"enqueued": False, "new_items": waiting}
            enqueue(
                session,
                self.clock,
                JobType.RUN_SCOUT,
                idempotency_key=f"scout-auto:{ctx.job.id}",
                params={"trigger": "auto"},
            )
        return {"enqueued": True, "new_items": waiting}
