"""The offline history snapshots (docs/13 §2): the same ``CustomerHistory`` as the online path,
built for every customer at once from dataframes of the `bank` tables, for training and
evaluation.

Each customer's snapshot holds their whole history; ``build_payment_features`` only looks at
what came before each payment (the leakage guard), so one snapshot serves all their payments.
The parity test checks that these snapshots give the online path's features.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy import Connection, Select, select

from delphi.sensing.config import AccountFeatureConfig, FeatureConfig
from delphi.sensing.features import (
    ACCOUNT_FEATURES,
    AccountHistory,
    CustomerHistory,
    IntArray,
    PaymentView,
    build_account_features,
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
        elif frame[column].map(lambda v: isinstance(v, list)).any():  # uuid[] columns
            frame[column] = frame[column].map(lambda v: tuple(str(i) for i in v or ()))
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
    complaints: pd.DataFrame


def load_bank_frames(conn: Connection) -> BankFrames:
    c, a, p, t = bank.customers, bank.accounts, bank.payments, bank.session_telemetry
    return BankFrames(
        customers=_frame(conn, select(c.c.id, c.c.age, c.c.is_vulnerable)),
        accounts=_frame(conn, select(a.c.id, a.c.customer_id, a.c.opened_at, a.c.kyc_level)),
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
                p.c.status,
                p.c.executed_at,
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
            select(
                bank.devices.c.id,
                bank.devices.c.customer_id,
                bank.devices.c.first_seen_at,
                bank.devices.c.fingerprint,
            ),
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
        complaints=_frame(
            conn,
            select(bank.complaints.c.reported_at, bank.complaints.c.counterparty_account_ids),
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
        self._first_seen: dict[str, int] = dict(
            zip(devices["id"], devices["first_us"].tolist(), strict=True)
        )
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
        if device is not None and device not in history.device_first_seen_us:
            first = self._first_seen.get(device)
            if first is not None:  # a device recorded under another customer
                history = _with_device(history, device, first)
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


def _with_device(history: CustomerHistory, device: str, first_seen_us: int) -> CustomerHistory:
    seen = {**history.device_first_seen_us, device: first_seen_us}
    return replace(history, device_first_seen_us=seen)


class OfflineAccountHistories:
    """Every account's snapshot (docs/13 §6), from the same frames, for the mule scorer."""

    def __init__(self, frames: BankFrames, config: AccountFeatureConfig) -> None:
        self.config = config
        accounts = frames.accounts
        opened = _us(accounts["opened_at"]).tolist()
        self._accounts: dict[str, tuple[int, str]] = {
            str(r.id): (t, str(r.kyc_level))
            for r, t in zip(accounts.itertuples(index=False), opened, strict=True)
        }
        payments = frames.payments
        fingerprint_of = dict(zip(frames.devices["id"], frames.devices["fingerprint"], strict=True))
        executed = payments[payments["status"] == config.executed_status]
        executed = executed.assign(at_us=_us(executed["executed_at"])).sort_values(
            ["at_us", "id"], kind="stable"
        )
        self._inbound = _by(executed, "payee_account_id")
        self._outbound = _by(executed, "payer_account_id")
        uses = payments.assign(
            at_us=_us(payments["initiated_at"]),
            fingerprint=payments["device_id"].map(fingerprint_of),
        ).sort_values(["at_us", "id"], kind="stable")
        self._uses = _by(uses, "payer_account_id")
        self._uses_by_print = _by(uses[uses["fingerprint"].notna()], "fingerprint")
        complaints = frames.complaints.assign(at_us=_us(frames.complaints["reported_at"]))
        named = complaints.explode("counterparty_account_ids").dropna(
            subset=["counterparty_account_ids"]
        )
        self._complaints = _by(named.sort_values("at_us"), "counterparty_account_ids")

    def snapshot(self, account_id: str) -> AccountHistory:
        if account_id not in self._accounts:
            raise LookupError(f"no account {account_id}")
        opened_us, kyc = self._accounts[account_id]
        inbound = self._inbound.get(account_id)
        outbound = self._outbound.get(account_id)
        own = self._uses.get(account_id)
        prints = (
            sorted({f for f in own["fingerprint"] if isinstance(f, str)}) if own is not None else []
        )
        shared = [self._uses_by_print[f] for f in prints if f in self._uses_by_print]
        others = pd.concat(shared).sort_values(["at_us", "id"], kind="stable") if shared else None
        if others is not None:
            others = others[others["payer_account_id"] != account_id]
        complaints = self._complaints.get(account_id)
        return AccountHistory(
            account_id=account_id,
            opened_at_us=opened_us,
            kyc_level=kyc,
            inbound_at_us=_ints(inbound, "at_us"),
            inbound_amount_paise=_ints(inbound, "amount_paise"),
            inbound_payer=_strs(inbound, "payer_account_id"),
            outbound_at_us=_ints(outbound, "at_us"),
            outbound_amount_paise=_ints(outbound, "amount_paise"),
            outbound_channel=_strs(outbound, "channel"),
            device_use_at_us=_ints(own, "at_us"),
            device_use_fingerprint=(
                tuple(_optional_str(f) for f in own["fingerprint"]) if own is not None else ()
            ),
            shared_use_at_us=_ints(others, "at_us"),
            shared_use_fingerprint=_strs(others, "fingerprint"),
            shared_use_account=_strs(others, "payer_account_id"),
            complaint_at_us=_ints(complaints, "at_us"),
        )

    def features(self, account_ids: Iterable[str], as_of_us: int) -> pd.DataFrame:
        """One row of account features per account, as of one time (``ACCOUNT_FEATURES``)."""
        ids = list(account_ids)
        rows = [build_account_features(self.snapshot(i), as_of_us, self.config) for i in ids]
        return pd.DataFrame(rows, index=pd.Index(ids, name="account_id"))[list(ACCOUNT_FEATURES)]


def _strs(frame: pd.DataFrame | None, column: str) -> tuple[str, ...]:
    return tuple(str(v) for v in frame[column]) if frame is not None else ()
