"""Typology drafts: reading and editing them (docs/11 §3, docs/16 "Scout", FR-SCT-03, #118).

A draft's ``spec`` (``DraftSpec``) is the new typology as the FDE engineer shapes it:
- key, display name, description, colour, F3 stages, contexts;
- the likelihood rows for the new hypothesis, starting from the cluster's empirical rows;
- ``open_mass_share`` (default 0.5, docs/11 §3);
- the loss block, copied from ``APP_INVESTMENT`` until edited;
- the sources it enables that ACTIVE has disabled, with a row for every hypothesis.

**The candidate KB** is ACTIVE plus the draft: the new hypothesis appended to the hypothesis order,
and each enabled source switched on with the given rows. Every save is validated by the KB's own
rules (``delphi.kb.validate``); a draft that breaks one is refused with the issues, unsaved.

**A save** (``edit_draft``):
- takes the draft's ``version`` (If-Match, optimistic lock);
- increments ``revision`` and returns the draft to DRAFT;
- adds the editor to ``editors`` (four-eyes promotion, docs/05 §7); audited.

A PROMOTED or REJECTED draft is final; a draft IN_SHADOW is not edited (its KB version is live).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from delphi.kb.issues import Issue
from delphi.kb.model import (
    Context,
    F3Stage,
    Hypothesis,
    Key,
    KnowledgeBase,
    LikelihoodRow,
    LossTable,
)
from delphi.kb.validate import validate_kb
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode, VersionConflictError
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.scout.tables import typology_drafts

DEFAULT_LOSS_FROM = "APP_INVESTMENT"  # docs/11 §3: the loss block's default
DEFAULT_OPEN_MASS_SHARE = 0.5  # docs/11 §3


class DraftStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    DRAFT = "DRAFT"
    BACKTESTED = "BACKTESTED"
    IN_SHADOW = "IN_SHADOW"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


EDITABLE = frozenset({DraftStatus.CANDIDATE, DraftStatus.DRAFT, DraftStatus.BACKTESTED})


class DraftSpec(BaseModel):
    """What a draft holds (``typology_drafts.spec``). ``name`` is the display name."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    key: Key | None = None
    description: str = ""
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    f3_stages: tuple[F3Stage, ...] = ()
    contexts: tuple[Context, ...] = ("OUTBOUND_PAYMENT",)
    likelihoods: dict[Key, LikelihoodRow] = Field(default_factory=dict)
    open_mass_share: float = Field(default=DEFAULT_OPEN_MASS_SHARE, ge=0, le=1)
    loss: LossTable | None = None  # None: APP_INVESTMENT's block
    # A source ACTIVE has disabled → a row for every hypothesis, the new key included.
    enabled_sources: dict[Key, dict[Key, LikelihoodRow]] = Field(default_factory=dict)
    outcome_profile: dict[str, dict[str, float]] = Field(default_factory=dict)  # the cluster's


class DraftPatch(BaseModel):
    """A partial update: the fields given replace the spec's (docs/16: partial update)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    key: Key | None = None
    description: str | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    f3_stages: tuple[F3Stage, ...] | None = None
    contexts: tuple[Context, ...] | None = None
    likelihoods: dict[Key, LikelihoodRow] | None = None
    open_mass_share: float | None = Field(default=None, ge=0, le=1)
    loss: LossTable | None = None
    enabled_sources: dict[Key, dict[Key, LikelihoodRow]] | None = None


class DraftInvalidError(DelphiError):
    """The edited draft breaks KB rules; nothing was saved."""

    code = ProblemCode.VALIDATION_FAILED

    def __init__(self, issues: list[Issue]) -> None:
        super().__init__(f"The draft breaks {len(issues)} KB rule(s); it was not saved.")
        self.issues = issues


def candidate_kb(
    active: KnowledgeBase, spec: DraftSpec
) -> tuple[KnowledgeBase | None, list[Issue]]:
    """ACTIVE + the draft (docs/11 §3), or the issues that stop it being built."""
    issues: list[Issue] = []
    if spec.key is None:
        issues.append(Issue("spec.key", "DRAFT_KEY", "The typology needs a key."))
    elif spec.key in active.hypotheses:
        issues.append(Issue("spec.key", "DRAFT_KEY", f"{spec.key} is already a hypothesis."))
    if spec.color is None:
        issues.append(Issue("spec.color", "DRAFT_COLOR", "The typology needs a colour."))
    for source in spec.enabled_sources:
        if source not in active.sources:
            issues.append(
                Issue(f"spec.enabled_sources.{source}", "DRAFT_SOURCE", "No such source.")
            )
        elif active.sources[source].enabled:
            issues.append(
                Issue(f"spec.enabled_sources.{source}", "DRAFT_SOURCE", "Already enabled.")
            )
    if issues:
        return None, issues
    assert spec.key is not None and spec.color is not None
    loss = spec.loss if spec.loss is not None else active.hypotheses[DEFAULT_LOSS_FROM].loss
    new_rows = dict(spec.likelihoods)
    hypotheses = dict(active.hypotheses)
    for source, rows in spec.enabled_sources.items():
        if spec.key in rows:
            new_rows[source] = rows[spec.key]
        for h, row in rows.items():
            if h in hypotheses:
                hyp = hypotheses[h]
                hypotheses[h] = hyp.model_copy(
                    update={"likelihoods": {**hyp.likelihoods, source: row}}
                )
    try:
        hypotheses[spec.key] = Hypothesis(
            key=spec.key,
            display_name=spec.name,
            kind="TYPOLOGY",
            contexts=spec.contexts,
            f3_stages=spec.f3_stages,
            color=spec.color,
            open_mass_share=spec.open_mass_share,
            description=spec.description,
            likelihoods=new_rows,
            loss=loss,
        )
    except ValidationError as err:
        return None, [
            Issue("spec." + ".".join(map(str, e["loc"])), "DRAFT_FIELD", e["msg"])
            for e in err.errors()
        ]
    sources = {
        k: s.model_copy(update={"enabled": True}) if k in spec.enabled_sources else s
        for k, s in active.sources.items()
    }
    kb = active.model_copy(
        update={
            "hypothesis_order": (*active.hypothesis_order, spec.key),
            "hypotheses": hypotheses,
            "sources": sources,
        }
    )
    return kb, validate_kb(kb, CLASSIFIER_NAMES)


def validate_draft(active: KnowledgeBase, spec: DraftSpec) -> list[Issue]:
    _kb, issues = candidate_kb(active, spec)
    return issues


def _row(session: Session, draft_id: UUID, *, lock: bool = False) -> Any:
    query = select(typology_drafts).where(typology_drafts.c.id == draft_id)
    row = session.execute(query.with_for_update() if lock else query).one_or_none()
    if row is None:
        raise NotFoundError("No typology draft with this id.")
    return row


def get_draft(session: Session, draft_id: UUID) -> Any:
    return _row(session, draft_id)


def list_drafts(
    session: Session,
    *,
    limit: int,
    status: DraftStatus | None = None,
    after: tuple[datetime, UUID] | None = None,
) -> list[Any]:
    """Newest edits first (keyset by ``(updated_at, id)``)."""
    t = typology_drafts.c
    query = select(typology_drafts)
    if status is not None:
        query = query.where(t.status == status.value)
    if after is not None:
        at, last = after
        query = query.where(or_(t.updated_at < at, and_(t.updated_at == at, t.id < last)))
    return list(session.execute(query.order_by(t.updated_at.desc(), t.id.desc()).limit(limit)))


def stored_spec(row: Any) -> DraftSpec:
    """The draft's spec; a CANDIDATE's (from RUN_SCOUT) holds name, likelihoods and the
    outcome profile only."""
    return DraftSpec.model_validate(row.spec)


def edit_draft(
    session: Session,
    clock: Clock,
    active: KnowledgeBase,
    draft_id: UUID,
    patch: DraftPatch,
    *,
    expected_version: int,
    by: UUID,
) -> Any:
    """docs/11 §3: apply ``patch``, validate ACTIVE + the result, and save it as the next
    revision (status DRAFT), in the caller's transaction."""
    row = _row(session, draft_id, lock=True)
    if row.version != expected_version:
        raise VersionConflictError("The draft changed since you loaded it; reload it first.")
    if DraftStatus(row.status) not in EDITABLE:
        raise DelphiError(
            f"A draft that is {row.status} cannot be edited.",
            code=ProblemCode.INVALID_STATE_TRANSITION,
        )
    merged: Mapping[str, Any] = {
        **stored_spec(row).model_dump(mode="json"),
        **patch.model_dump(mode="json", exclude_unset=True),
    }
    try:
        spec = DraftSpec.model_validate(merged)
    except ValidationError as err:
        raise DraftInvalidError(
            [
                Issue("spec." + ".".join(map(str, e["loc"])), "DRAFT_FIELD", e["msg"])
                for e in err.errors()
            ]
        ) from None
    issues = validate_draft(active, spec)
    if issues:
        raise DraftInvalidError(issues)
    now = clock.now()
    editors = list(row.editors) if by in row.editors else [*row.editors, by]
    t = typology_drafts.c
    session.execute(
        update(typology_drafts)
        .where(t.id == draft_id)
        .values(
            spec=spec.model_dump(mode="json"),
            status=DraftStatus.DRAFT.value,
            revision=row.revision + 1,
            last_edited_by=by,
            editors=editors,
            updated_at=now,
            version=row.version + 1,
        )
    )
    record(
        session,
        clock,
        actor_type=ActorType.USER,
        actor_id=str(by),
        action="scout.draft.edited",
        entity_type="typology_draft",
        entity_id=str(draft_id),
        payload={"revision": row.revision + 1, "fields": sorted(patch.model_fields_set)},
    )
    return _row(session, draft_id)
