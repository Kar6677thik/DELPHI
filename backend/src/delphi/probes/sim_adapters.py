"""Simulated passive-probe adapters over the `bank` schema (docs/04 §4.2, docs/08 §5).

They read the simulated bank through the tables `delphi.sim.api` re-exports (docs/04 §4.1
rule 3), as the `delphi_app` role, and never `sim_truth`, so they run in the api as well as
the worker. Each read is one short transaction whose `statement_timeout` is set just above the
passive timeout, so a query the runner has given up on ends soon instead of holding a pool
connection. Only data from before the payment's initiation (`as_of`) is read.

A missing row (no telemetry for the payment, an unknown device or payee) raises `LookupError`,
which the runner records as an ERROR result.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import Connection, Engine, func, select

from delphi.probes.classifiers import (
    BeneficiaryIntelObs,
    CallStatusObs,
    DevicePostureObs,
    PhoneRiskObs,
    SessionBehaviourObs,
)
from delphi.probes.passive import (
    PassiveRequest,
    SessionTelemetry,
    call_status_obs,
    device_posture_obs,
    session_behaviour_obs,
)
from delphi.sim import api as sim

# Earlier payments that count towards an established payee: approved ones only, not those
# declined, cancelled, held or still waiting for the Guardian.
COUNTED_PAYMENT_STATUSES = ("RELEASED", "EXECUTED")


@contextmanager
def _read(engine: Engine, statement_timeout_ms: int) -> Iterator[Connection]:
    with engine.connect() as conn, conn.begin():
        conn.execute(select(func.set_config("statement_timeout", str(statement_timeout_ms), True)))
        yield conn


def _payee_account(conn: Connection, account_id: UUID) -> tuple[bool, str | None]:
    row = conn.execute(
        select(sim.accounts.c.is_merchant, sim.accounts.c.phone_number).where(
            sim.accounts.c.id == account_id
        )
    ).one_or_none()
    if row is None:
        raise LookupError(f"payee account {account_id} not found")
    return bool(row.is_merchant), row.phone_number


class MuleScorePort(Protocol):
    """p_mule(account) as of a time (docs/13 §6). Implemented by the sensing layer (#91)."""

    def p_mule(self, account_id: UUID, as_of: datetime) -> float: ...


class SimBeneficiaryIntel:
    """BENEFICIARY_INTEL: the sim suspect registry, the mule score and the payee's history."""

    def __init__(self, engine: Engine, mule_scores: MuleScorePort, statement_timeout_ms: int):
        self._engine = engine
        self._mule_scores = mule_scores
        self._timeout_ms = statement_timeout_ms

    def beneficiary_intel(self, request: PassiveRequest) -> BeneficiaryIntelObs:
        payee = request.payee_account_id
        with _read(self._engine, self._timeout_ms) as conn:
            is_merchant, _ = _payee_account(conn, payee)
            listed = conn.execute(
                select(sim.sim_registry_listings.c.account_id).where(
                    sim.sim_registry_listings.c.account_id == payee,
                    sim.sim_registry_listings.c.listed_at < request.as_of,
                )
            ).first()
            history = conn.execute(
                select(
                    func.count(),
                    func.count(sim.accounts.c.customer_id.distinct()),
                    func.min(sim.payments.c.initiated_at),
                )
                .select_from(
                    sim.payments.join(
                        sim.accounts, sim.accounts.c.id == sim.payments.c.payer_account_id
                    )
                )
                .where(
                    sim.payments.c.payee_account_id == payee,
                    sim.payments.c.initiated_at < request.as_of,
                    sim.payments.c.status.in_(COUNTED_PAYMENT_STATUSES),
                )
            ).one()
        return BeneficiaryIntelObs(
            listed_in_suspect_registry=listed is not None,
            p_mule=self._mule_scores.p_mule(payee, request.as_of),
            is_registered_merchant=is_merchant,
            earlier_payments=int(history[0]),
            earlier_distinct_customers=int(history[1]),
            first_payment_at=history[2],
            as_of=request.as_of,
        )


class SimPhoneRisk:
    """COUNTERPARTY_PHONE_RISK: the sim FRI category of the phone linked to the payee VPA."""

    def __init__(self, engine: Engine, statement_timeout_ms: int):
        self._engine = engine
        self._timeout_ms = statement_timeout_ms

    def phone_risk(self, request: PassiveRequest) -> PhoneRiskObs:
        with _read(self._engine, self._timeout_ms) as conn:
            _, phone = _payee_account(conn, request.payee_account_id)
            if phone is None:
                return PhoneRiskObs(fri_category=None)
            category = conn.execute(
                select(sim.sim_phone_risk.c.category).where(
                    sim.sim_phone_risk.c.phone_number == phone
                )
            ).scalar_one_or_none()
        return PhoneRiskObs(fri_category=category)


class SimSessionTelemetry:
    """DEVICE_POSTURE, SESSION_BEHAVIOUR and CALL_STATUS, from `bank.session_telemetry` (the
    mobile SDK's signals in the simulation) and `bank.devices`."""

    def __init__(self, engine: Engine, statement_timeout_ms: int):
        self._engine = engine
        self._timeout_ms = statement_timeout_ms

    def _telemetry(self, conn: Connection, payment_id: UUID) -> SessionTelemetry:
        t = sim.session_telemetry.c
        row = conn.execute(
            select(
                t.typing_cadence_z,
                t.paste_on_account_field,
                t.max_field_dwell_s,
                t.remote_access_active,
                t.screen_share_active,
                t.on_call,
                t.call_counterparty_known,
            ).where(t.payment_id == payment_id)
        ).one_or_none()
        if row is None:
            raise LookupError(f"no session telemetry for payment {payment_id}")
        return SessionTelemetry(*row)

    def device_posture(self, request: PassiveRequest) -> DevicePostureObs:
        if request.device_id is None:
            raise LookupError(f"payment {request.payment_id} has no device")
        with _read(self._engine, self._timeout_ms) as conn:
            telemetry = self._telemetry(conn, request.payment_id)
            first_seen = conn.execute(
                select(sim.devices.c.first_seen_at).where(sim.devices.c.id == request.device_id)
            ).scalar_one_or_none()
        if first_seen is None:
            raise LookupError(f"device {request.device_id} not found")
        return device_posture_obs(telemetry, first_seen, request.as_of)

    def session_behaviour(self, request: PassiveRequest) -> SessionBehaviourObs:
        with _read(self._engine, self._timeout_ms) as conn:
            return session_behaviour_obs(self._telemetry(conn, request.payment_id))

    def call_status(self, request: PassiveRequest) -> CallStatusObs:
        with _read(self._engine, self._timeout_ms) as conn:
            return call_status_obs(self._telemetry(conn, request.payment_id))
