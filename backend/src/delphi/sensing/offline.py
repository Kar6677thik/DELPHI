"""The offline history snapshots (docs/13 §2): the same ``CustomerHistory`` as the online path,
built for every customer at once from dataframes of the `bank` tables, for training and
evaluation.

Each customer's snapshot holds their whole history; ``build_payment_features`` only looks at
what came before each payment (the leakage guard), so one snapshot serves all their payments.
The parity test checks that these snapshots give the online path's features.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy import Connection, Select, select

from delphi.sensing.config import FeatureConfig
from delphi.sensing.features import (
    CustomerHistory,
    IntArray,
    PaymentView,
    build_payment_features,
    payment_feature_names,
)
from delphi.sim import api as bank

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
_US = pd.Timedelta(microseconds=1)


def _us(times: pd.Series[Any]) -> IntArray:
    """Timestamps as integer microseconds since the epoch (exact, like ``features.to_us``)."""
    stamps = pd.to_datetime(times, utc=True)
    return np.asarray(((stamps - _EPOCH) // _US).to_numpy(), dtype=np.int64)


@dataclass(frozen=True, slots=True)
class _Payment:
    """A payment row, typed, as the offline snapshot needs it."""

    payer_account_id: str
    payee_account_id: str
    amount_paise: int
    channel: str
    at_us: int
    device_id: str | None
    customer_id: str | None
    session_duration_s: float | None
    remote_access_active: bool | None


# itertuples gives loosely typed scalars; these say what each column holds.
def _int(value: Any) -> int:
    return int(value)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_float(value: Any) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _optional_bool(value: Any) -> bool | None:
    return None if value is None or pd.isna(value) else bool(value)


def _frame(conn: Connection, query: Select[Any]) -> pd.DataFrame:
    result = conn.execute(query)
    frame = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    for column in frame.columns:  # ids as strings, as the online path keys them
        if frame[column].map(lambda v: isinstance(v, UUID)).any():
            frame[column] = frame[column].map(lambda v: str(v) if isinstance(v, UUID) else None)
    return frame


@dataclass(frozen=True, slots=True)
class BankFrames:
    """The `bank` rows the payment features read, one dataframe per table."""

    customers: pd.DataFrame
    accounts: pd.DataFrame
    payments: pd.DataFrame
    telemetry: pd.DataFrame
    payees: pd.DataFrame
    devices: pd.DataFrame
    logins: pd.DataFrame
    crm_events: pd.DataFrame


def load_bank_frames(conn: Connection) -> BankFrames:
    c, a, p, t = bank.customers, bank.accounts, bank.payments, bank.session_telemetry
    return BankFrames(
        customers=_frame(conn, select(c.c.id, c.c.age, c.c.is_vulnerable)),
        accounts=_frame(conn, select(a.c.id, a.c.customer_id, a.c.opened_at)),
        payments=_frame(
            conn,
            select(
                p.c.id,
                p.c.payer_account_id,
                p.c.payee_account_id,
                p.c.amount_paise,
                p.c.channel,
                p.c.initiated_at,
                p.c.device_id,
            ),
        ),
        telemetry=_frame(
            conn, select(t.c.payment_id, t.c.session_duration_s, t.c.remote_access_active)
        ),
        payees=_frame(
            conn,
            select(bank.payees.c.customer_id, bank.payees.c.account_id, bank.payees.c.added_at),
        ),
        devices=_frame(
            conn,
            select(bank.devices.c.id, bank.devices.c.customer_id, bank.devices.c.first_seen_at),
        ),
        logins=_frame(
            conn,
            select(
                bank.login_events.c.id,
                bank.login_events.c.customer_id,
                bank.login_events.c.at,
                bank.login_events.c.city,
            ),
        ),
        crm_events=_frame(
            conn,
            select(bank.crm_events.c.customer_id, bank.crm_events.c.type, bank.crm_events.c.at),
        ),
    )


class OfflineHistories:
    """Every customer's snapshot, and the payments to score with them."""

    def __init__(self, frames: BankFrames, config: FeatureConfig) -> None:
        self.config = config
        accounts = frames.accounts
        self._customer_of: dict[str, str | None] = dict(
            zip(accounts["id"], accounts["customer_id"], strict=True)
        )
        self._opened_us: dict[str, int] = dict(
            zip(accounts["id"], _us(accounts["opened_at"]).tolist(), strict=True)
        )
        payments = frames.payments.assign(
            at_us=_us(frames.payments["initiated_at"]),
            customer_id=frames.payments["payer_account_id"].map(self._customer_of),
        )
        telemetry = frames.telemetry.set_index("payment_id")
        joined = payments.join(telemetry, on="id")
        self._payments: dict[str, _Payment] = {
            str(r.id): _Payment(
                payer_account_id=str(r.payer_account_id),
                payee_account_id=str(r.payee_account_id),
                amount_paise=_int(r.amount_paise),
                channel=str(r.channel),
                at_us=_int(r.at_us),
                device_id=_optional_str(r.device_id),
                customer_id=_optional_str(r.customer_id),
                session_duration_s=_optional_float(r.session_duration_s),
                remote_access_active=_optional_bool(r.remote_access_active),
            )
            for r in joined.itertuples(index=False)
        }

        own = payments[payments["customer_id"].notna()].sort_values(["at_us", "id"], kind="stable")
        by_customer = {k: g for k, g in own.groupby("customer_id", sort=False)}
        payees = _by(frames.payees, "customer_id")
        devices = frames.devices.assign(first_us=_us(frames.devices["first_seen_at"]))
        devices_by = _by(devices, "customer_id")
        logins = frames.logins.assign(at_us=_us(frames.logins["at"])).sort_values(
            ["at_us", "id"], kind="stable"
        )
        logins_by = _by(logins, "customer_id")
        crm = frames.crm_events
        limits = crm[crm["type"].isin(config.limit_increase_event_types)]
        limits_by = _by(limits.assign(at_us=_us(limits["at"])).sort_values("at_us"), "customer_id")

        self._histories: dict[str, CustomerHistory] = {}
        for row in frames.customers.itertuples(index=False):
            cid = str(row.id)
            pays = by_customer.get(cid)
            saved = payees.get(cid)
            owned = devices_by.get(cid)
            seen = logins_by.get(cid)
            raised = limits_by.get(cid)
            self._histories[cid] = CustomerHistory(
                customer_id=cid,
                age_years=_int(row.age),
                is_vulnerable=bool(row.is_vulnerable),
                payment_at_us=_ints(pays, "at_us"),
                payment_amount_paise=_ints(pays, "amount_paise"),
                payment_payee=tuple(pays["payee_account_id"]) if pays is not None else (),
                payee_added_at_us=(
                    dict(zip(saved["account_id"], _us(saved["added_at"]).tolist(), strict=True))
                    if saved is not None
                    else {}
                ),
                device_first_seen_us=(
                    dict(zip(owned["id"], owned["first_us"].tolist(), strict=True))
                    if owned is not None
                    else {}
                ),
                login_at_us=_ints(seen, "at_us"),
                login_city=tuple(seen["city"]) if seen is not None else (),
                limit_increase_at_us=_ints(raised, "at_us"),
            )

    def snapshot(self, payment_id: str) -> tuple[PaymentView, CustomerHistory]:
        row = self._payments[payment_id]
        if row.customer_id is None:
            raise LookupError(f"payment {payment_id} is not made by a customer of the bank")
        history = self._histories[row.customer_id]
        device = row.device_id
        payment = PaymentView(
            id=payment_id,
            payer_account_id=row.payer_account_id,
            payee_account_id=row.payee_account_id,
            amount_paise=row.amount_paise,
            channel=row.channel,
            initiated_at_us=row.at_us,
            device_id=device,
            payee_account_opened_at_us=self._opened_us[row.payee_account_id],
            payee_p_mule=self.config.mule_score_fallback,  # until the mule scorer (#91)
            session_duration_s=row.session_duration_s,
            remote_access_active=row.remote_access_active,
        )
        return payment, history

    def features(self, payment_ids: Iterable[str]) -> pd.DataFrame:
        """One row of payment features per payment, columns in ``payment_feature_names``."""
        ids = list(payment_ids)
        rows = [build_payment_features(*self.snapshot(i), self.config) for i in ids]
        return pd.DataFrame(rows, index=pd.Index(ids, name="payment_id"))[
            list(payment_feature_names(self.config))
        ]


def _by(frame: pd.DataFrame, key: str) -> Mapping[str, pd.DataFrame]:
    return {str(k): g for k, g in frame.groupby(key, sort=False)}


def _ints(frame: pd.DataFrame | None, column: str) -> IntArray:
    if frame is None:
        return np.zeros(0, dtype=np.int64)
    return np.asarray(frame[column].to_numpy(), dtype=np.int64)
