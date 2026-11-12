"""The payment features (docs/13 §2–3, FR-SNS-01): one function over a history snapshot.

``build_payment_features(payment, history, config)`` is the only place a payment feature is
computed. The online path (``delphi.sensing.history``) loads the snapshot from PostgreSQL; the
offline path (``delphi.sensing.offline``) builds the same snapshots from dataframes. So training
and serving cannot drift apart (FR-SNS-02, the parity test).

**Leakage guard.** ``as_of`` is the payment's initiation time, and only events strictly before
it count: every window below is cut with ``searchsorted(..., as_of, "left")``, and a payee or
device first seen at or after ``as_of`` counts as not there yet. A snapshot may therefore hold
later events (the offline one holds the customer's whole history) without changing a feature.

Times are integer microseconds since the Unix epoch (UTC), amounts integer paise.

Where docs/13 §3 is silent (the gap fills, also listed there):
- windows count the customer's earlier payments from any of their accounts, whatever their
  status, and never the payment being scored;
- with no earlier payment, ``amount_z_30d`` and ``hour_deviation`` are 0 and
  ``amount_ratio_max_lifetime`` is 1;
- ``hour_deviation`` is the circular distance from the circular median local hour of the
  earlier payments: the hours are cut at the largest empty arc of the clock, so a customer who
  pays around midnight has a median near midnight, not near noon;
- ``device_age_days`` counts only the customer's own devices (``bank.devices.customer_id``). A
  device recorded under another customer has never been this customer's, so it is new to them
  (age 0): the account-takeover case DEVICE_POSTURE is there to catch;
- ``sum_24h_ratio_30d`` divides by the 30-day daily mean, floored like the z-score's std;
- ``login_new_city`` is 0 when the last login is the customer's first (TD-8);
- ``limit_change_24h`` stays 0: the bank records no limit changes yet.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from delphi.sensing.config import AccountFeatureConfig, FeatureConfig

IntArray = NDArray[np.int64]

_US_PER_SECOND = 1_000_000
_US_PER_MINUTE = 60 * _US_PER_SECOND
_US_PER_HOUR = 60 * _US_PER_MINUTE
_US_PER_DAY = 24 * _US_PER_HOUR
_HOURS_PER_DAY = 24
_PAISE_PER_INR = 100
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TEN_MINUTES_US = 10 * _US_PER_MINUTE
_NEW_PAYEES_DAYS = 7  # docs/13 §3 `new_payees_7d`

# Inputs to a passive-probe classifier: observations, so never inputs to the prior model
# (docs/13 §3, the double-count guard). They are still computed for the Scout and the DD pack.
PROBE_INPUT_FEATURES = frozenset(
    {"is_new_device", "device_age_days", "remote_access_active", "payee_p_mule"}
)


def to_us(at: datetime) -> int:
    """A timezone-aware time as integer microseconds since the epoch (exact)."""
    if at.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    return (at - _EPOCH) // timedelta(microseconds=1)


def payment_feature_names(config: FeatureConfig) -> tuple[str, ...]:
    """Every payment feature, in a fixed order (the model's columns follow it)."""
    return (
        "amount_log10",
        "amount_z_30d",
        "amount_ratio_max_lifetime",
        "hour_sin",
        "hour_cos",
        "hour_deviation",
        "is_new_payee",
        "payee_age_days",
        "payee_account_age_days",
        "payee_p_mule",
        "device_age_days",
        "is_new_device",
        "count_10m",
        "count_24h",
        "sum_24h_ratio_30d",
        "distinct_payees_24h",
        "new_payees_7d",
        "limit_change_24h",
        "login_new_city",
        "age_years",
        "is_vulnerable",
        *(f"channel_{c}" for c in config.channels),
        "session_duration_s",
        "remote_access_active",
    )


def prior_feature_names(config: FeatureConfig) -> tuple[str, ...]:
    """The prior model's inputs: every payment feature but the probe inputs."""
    return tuple(f for f in payment_feature_names(config) if f not in PROBE_INPUT_FEATURES)


@dataclass(frozen=True, slots=True)
class PaymentView:
    """The payment being scored, with what the bank knows about its payee and session."""

    id: str
    payer_account_id: str
    payee_account_id: str
    amount_paise: int
    channel: str
    initiated_at_us: int
    device_id: str | None
    payee_account_opened_at_us: int
    payee_p_mule: float
    session_duration_s: float | None  # from session telemetry; None without a session
    remote_access_active: bool | None


def _empty() -> IntArray:
    return np.zeros(0, dtype=np.int64)


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    """What the bank knew about the paying customer. Each event array is sorted by time; later
    events may be present (they are ignored, see the leakage guard)."""

    customer_id: str
    age_years: int
    is_vulnerable: bool
    # The customer's payments, as payer, from any of their accounts.
    payment_at_us: IntArray = field(default_factory=_empty)
    payment_amount_paise: IntArray = field(default_factory=_empty)
    payment_payee: tuple[str, ...] = ()
    # Saved payees: payee account → when it was saved.
    payee_added_at_us: Mapping[str, int] = field(default_factory=dict)
    # The customer's devices: device → first seen.
    device_first_seen_us: Mapping[str, int] = field(default_factory=dict)
    login_at_us: IntArray = field(default_factory=_empty)
    login_city: tuple[str, ...] = ()
    limit_increase_at_us: IntArray = field(default_factory=_empty)
    # Derived: each payee's first index in ``payment_payee`` ("paid before?" in O(1)).
    first_payment_index: Mapping[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        first: dict[str, int] = {}
        for i, payee in enumerate(self.payment_payee):
            first.setdefault(payee, i)
        object.__setattr__(self, "first_payment_index", first)


def _days(us: int) -> float:
    return us / _US_PER_DAY


def _local_hours(at_us: IntArray | int, offset_minutes: int) -> NDArray[np.float64]:
    local = np.asarray(at_us, dtype=np.int64) + offset_minutes * _US_PER_MINUTE
    return (local % _US_PER_DAY).astype(np.float64) / _US_PER_HOUR


def circular_median(hours: NDArray[np.float64]) -> float:
    """The median of clock hours, cut at the largest empty arc of the 24-hour clock.

    The hours are rotated so the cut is at 0, the ordinary median is taken, and the result is
    rotated back. [23.5, 23.7, 0.5, 0.6] gives about 0.1, not the linear median's 12.
    """
    ordered = np.sort(np.asarray(hours, dtype=np.float64) % _HOURS_PER_DAY)
    gaps = np.diff(ordered, append=ordered[0] + _HOURS_PER_DAY)  # the last one wraps around
    cut = ordered[(int(np.argmax(gaps)) + 1) % len(ordered)]
    rotated = (ordered - cut) % _HOURS_PER_DAY
    return float((np.median(rotated) + cut) % _HOURS_PER_DAY)


def _since(at_us: IntArray, start_us: int, end: int) -> slice:
    """The indices of events with ``start_us <= at < end`` (``end`` = the earlier-events cut)."""
    return slice(int(np.searchsorted(at_us, start_us, "left")), end)


def build_payment_features(
    payment: PaymentView, history: CustomerHistory, config: FeatureConfig
) -> dict[str, float]:
    """docs/13 §3, for one payment, as of its initiation time."""
    as_of = payment.initiated_at_us
    amount_inr = payment.amount_paise / _PAISE_PER_INR
    floor = config.amount_floor_inr
    baseline_us = config.baseline_days * _US_PER_DAY

    # The customer's earlier payments (strictly before as_of).
    n = int(np.searchsorted(history.payment_at_us, as_of, "left"))
    at, amounts_inr = history.payment_at_us[:n], history.payment_amount_paise[:n] / _PAISE_PER_INR
    in_30d = amounts_inr[_since(at, as_of - baseline_us, n)]
    day = _since(at, as_of - _US_PER_DAY, n)
    in_24h = amounts_inr[day]
    if in_30d.size:
        amount_z_30d = (amount_inr - float(in_30d.mean())) / max(float(in_30d.std()), floor)
    else:
        amount_z_30d = 0.0
    lifetime_max = float(amounts_inr.max()) if n else 0.0
    daily_mean_30d = float(in_30d.sum()) / config.baseline_days

    hour = float(_local_hours(as_of, config.local_utc_offset_minutes))
    if n:
        median = circular_median(_local_hours(at, config.local_utc_offset_minutes))
        gap = abs(hour - median) % _HOURS_PER_DAY
        hour_deviation = min(gap, _HOURS_PER_DAY - gap)
    else:
        hour_deviation = 0.0

    # The payee: saved when, and paid before?
    payee = payment.payee_account_id
    added = history.payee_added_at_us.get(payee)
    saved = added is not None and added < as_of
    ever_paid = history.first_payment_index.get(payee, n) < n
    if not saved:
        is_new_payee = True
        payee_age_us = 0
    else:
        assert added is not None
        payee_age_us = as_of - added
        is_new_payee = payee_age_us < config.new_payee_hours * _US_PER_HOUR or (
            not ever_paid and payee_age_us < baseline_us
        )
    week_ago = as_of - _NEW_PAYEES_DAYS * _US_PER_DAY
    new_payees_7d = sum(1 for t in history.payee_added_at_us.values() if week_ago <= t < as_of)

    # The device the session runs on: only the customer's own devices have an age.
    first_seen = history.device_first_seen_us.get(payment.device_id or "")
    device_age_us = as_of - first_seen if first_seen is not None and first_seen < as_of else 0
    device_age_days = min(_days(device_age_us), config.device_age_cap_days)

    # Logins: is the last one, before this payment, from a city never seen before?
    logins = int(np.searchsorted(history.login_at_us, as_of, "left"))
    cities = history.login_city[:logins]
    login_new_city = logins > 1 and cities[-1] not in cities[:-1]

    limits = history.limit_increase_at_us
    limit_change_24h = _count(limits, as_of) > _count(limits, as_of - _US_PER_DAY)

    features: dict[str, float] = {
        "amount_log10": math.log10(amount_inr),
        "amount_z_30d": amount_z_30d,
        "amount_ratio_max_lifetime": amount_inr / lifetime_max if n else 1.0,
        "hour_sin": math.sin(2 * math.pi * hour / _HOURS_PER_DAY),
        "hour_cos": math.cos(2 * math.pi * hour / _HOURS_PER_DAY),
        "hour_deviation": hour_deviation,
        "is_new_payee": float(is_new_payee),
        "payee_age_days": min(_days(payee_age_us), config.payee_age_cap_days),
        "payee_account_age_days": min(
            max(_days(as_of - payment.payee_account_opened_at_us), 0.0),
            config.payee_account_age_cap_days,
        ),
        "payee_p_mule": payment.payee_p_mule,
        "device_age_days": device_age_days,
        "is_new_device": float(device_age_days < config.new_device_days),
        "count_10m": float(n - int(np.searchsorted(at, as_of - _TEN_MINUTES_US, "left"))),
        "count_24h": float(in_24h.size),
        "sum_24h_ratio_30d": float(in_24h.sum()) / max(daily_mean_30d, floor),
        "distinct_payees_24h": float(len(set(history.payment_payee[day]))),
        "new_payees_7d": float(new_payees_7d),
        "limit_change_24h": float(limit_change_24h),
        "login_new_city": float(login_new_city),
        "age_years": float(history.age_years),
        "is_vulnerable": float(history.is_vulnerable),
        **{f"channel_{c}": float(payment.channel == c) for c in config.channels},
        "session_duration_s": payment.session_duration_s or 0.0,
        "remote_access_active": float(bool(payment.remote_access_active)),
    }
    return features


def _count(at_us: IntArray, as_of: int) -> int:
    return int(np.searchsorted(at_us, as_of, "left"))


# --------------------------------------------------------------------------- account features

ACCOUNT_FEATURES = (
    "inbound_distinct_payers_48h",
    "inbound_count_7d",
    "inbound_sum_7d_log",
    "median_dwell_minutes_7d",
    "outbound_to_inbound_ratio_7d",
    "account_age_days",
    "kyc_minimal",
    "device_shared_accounts_30d",
    "cash_out_share_7d",
    "complaints_linked",
)


@dataclass(frozen=True, slots=True)
class AccountHistory:
    """What the bank knew about an account (any bank's: docs/13 §6, A-06). Each event array is
    sorted by time; later events may be present and are ignored.

    - ``inbound_*`` / ``outbound_*``: its executed credits and debits, by execution time;
    - ``device_use_*``: its own payments (any status) by initiation time, and the device
      fingerprint each was made from (``None`` without a device);
    - ``shared_use_*``: payments by *other* accounts from a fingerprint this account used;
    - ``complaint_at_us``: complaints that name the account as a counterparty.
    """

    account_id: str
    opened_at_us: int
    kyc_level: str
    inbound_at_us: IntArray = field(default_factory=_empty)
    inbound_amount_paise: IntArray = field(default_factory=_empty)
    inbound_payer: tuple[str, ...] = ()
    outbound_at_us: IntArray = field(default_factory=_empty)
    outbound_amount_paise: IntArray = field(default_factory=_empty)
    outbound_channel: tuple[str, ...] = ()
    device_use_at_us: IntArray = field(default_factory=_empty)
    device_use_fingerprint: tuple[str | None, ...] = ()
    shared_use_at_us: IntArray = field(default_factory=_empty)
    shared_use_fingerprint: tuple[str, ...] = ()
    shared_use_account: tuple[str, ...] = ()
    complaint_at_us: IntArray = field(default_factory=_empty)


def build_account_features(
    history: AccountHistory, as_of_us: int, config: AccountFeatureConfig
) -> dict[str, float]:
    """docs/13 §6, for one account, as of ``as_of_us``. Gap fills (also in docs/13 §6):
    ``inbound_sum_7d_log`` is log10(1 + ₹ sum); ``median_dwell_minutes_7d`` is the median time
    from each 7-day credit to the account's next debit, credits not yet followed by one left
    out, and the whole window when none is; ``outbound_to_inbound_ratio_7d`` floors the inbound
    sum at ₹100; ``cash_out_share_7d`` is the share of the 7-day debit amount cashed out;
    ``device_shared_accounts_30d`` counts other accounts that paid, in 30 days, from a device
    fingerprint this account paid from in those 30 days."""
    flow_us = int(config.flow_days * _US_PER_DAY)
    week = as_of_us - flow_us

    n_in = _count(history.inbound_at_us, as_of_us)
    in_at = history.inbound_at_us[:n_in]
    recent_in = slice(_count(in_at, week), n_in)
    in_7d = history.inbound_amount_paise[recent_in] / _PAISE_PER_INR
    payers_from = _count(in_at, as_of_us - int(config.inbound_payers_hours * _US_PER_HOUR))
    payers = set(history.inbound_payer[payers_from:n_in])

    n_out = _count(history.outbound_at_us, as_of_us)
    out_at = history.outbound_at_us[:n_out]
    recent_out = slice(_count(out_at, week), n_out)
    out_7d = history.outbound_amount_paise[recent_out] / _PAISE_PER_INR
    cashed = sum(
        amount
        for amount, channel in zip(out_7d, history.outbound_channel[recent_out], strict=True)
        if channel == config.cash_out_channel
    )

    dwells = []
    for credit in in_at[recent_in]:
        nxt = int(np.searchsorted(out_at, credit, "right"))  # the first debit after the credit
        if nxt < n_out:
            dwells.append((int(out_at[nxt]) - int(credit)) / _US_PER_MINUTE)
    dwell = float(np.median(dwells)) if dwells else flow_us / _US_PER_MINUTE

    share_from = as_of_us - int(config.device_share_days * _US_PER_DAY)
    own = slice(
        _count(history.device_use_at_us, share_from), _count(history.device_use_at_us, as_of_us)
    )
    prints = {f for f in history.device_use_fingerprint[own] if f is not None}
    others = slice(
        _count(history.shared_use_at_us, share_from), _count(history.shared_use_at_us, as_of_us)
    )
    sharers = {
        account
        for account, fingerprint in zip(
            history.shared_use_account[others], history.shared_use_fingerprint[others], strict=True
        )
        if fingerprint in prints and account != history.account_id
    }

    in_sum, out_sum = float(in_7d.sum()), float(out_7d.sum())
    return {
        "inbound_distinct_payers_48h": float(len(payers)),
        "inbound_count_7d": float(in_7d.size),
        "inbound_sum_7d_log": math.log10(1 + in_sum),
        "median_dwell_minutes_7d": dwell,
        "outbound_to_inbound_ratio_7d": out_sum / max(in_sum, config.amount_floor_inr),
        "account_age_days": min(
            max(_days(as_of_us - history.opened_at_us), 0.0), config.account_age_cap_days
        ),
        "kyc_minimal": float(history.kyc_level == config.minimal_kyc_level),
        "device_shared_accounts_30d": float(len(sharers)),
        "cash_out_share_7d": cashed / out_sum if out_sum else 0.0,
        "complaints_linked": float(_count(history.complaint_at_us, as_of_us)),
    }
