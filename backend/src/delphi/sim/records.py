"""Generated rows shared by the history and the fraud scenarios (docs/17 §3)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ScenarioInstance:
    """A row of ``sim_truth.scenario_instances``."""

    id: UUID
    scenario_type: str
    hypothesis_key: str
    params: Mapping[str, Any]
    started_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduledTransfer:
    """A ``bank.sim_schedule`` TRANSFER row (docs/14 §8): a payment that falls due after the
    history, made later by the live simulation or the evaluation replay."""

    id: UUID
    due_at: datetime
    kind: str
    payload: Mapping[str, Any]
    done_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ScheduleTruth:
    """A ``sim_truth.schedule_truth`` row: why a TRANSFER row exists. The app-visible payload
    carries only what executing the payment needs (#56)."""

    schedule_id: UUID
    reason: str  # SALARY or MULE_FORWARDING
    flow: int | None  # the mule-ring flow it continues
    session: bool  # a home-bank mule's app session


@dataclass(frozen=True, slots=True)
class CrmEvent:
    """A row of ``bank.crm_events``."""

    id: UUID
    customer_id: UUID
    type: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Complaint:
    """A row of ``bank.complaints``."""

    id: UUID
    reported_at: datetime
    victim_customer_id: UUID
    channel: str
    category: str
    payment_ids: tuple[UUID, ...]
    counterparty_account_ids: tuple[UUID, ...]
