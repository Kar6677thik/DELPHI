"""The payment features (docs/13 §3, FR-SNS-01) on hand-built histories."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sensing.config import FeatureConfig, load_sensing_config
from delphi.sensing.features import (
    PROBE_INPUT_FEATURES,
    CustomerHistory,
    PaymentView,
    build_payment_features,
    circular_median,
    payment_feature_names,
    prior_feature_names,
    to_us,
)

AS_OF = datetime(2026, 7, 1, 6, 30, tzinfo=UTC)  # 12:00 IST
PAYEE, OTHER = "payee-1", "payee-2"


@pytest.fixture(scope="module")
def config() -> FeatureConfig:
    return load_sensing_config().features


def _at(**delta: float) -> int:
    return to_us(AS_OF - timedelta(**delta))


def _payment(**overrides: object) -> PaymentView:
    base = PaymentView(
        id="p",
        payer_account_id="acc",
        payee_account_id=PAYEE,
        amount_paise=1_000_000,  # ₹10,000
        channel="IMPS",
        initiated_at_us=to_us(AS_OF),
        device_id="dev",
        payee_account_opened_at_us=_at(days=400),
        payee_p_mule=0.1,
        session_duration_s=42.0,
        remote_access_active=False,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _history(**overrides: object) -> CustomerHistory:
    payments = [  # (days ago, ₹, payee)
        (100, 2_000, OTHER),
        (20, 1_000, PAYEE),
        (10, 3_000, OTHER),
        (0.5, 500, OTHER),  # 12 h ago
        (1 / 288, 100, PAYEE),  # 5 min ago
    ]
    base = CustomerHistory(
        customer_id="c",
        age_years=72,
        is_vulnerable=True,
        payment_at_us=np.array([_at(days=d) for d, _, _ in payments], dtype=np.int64),
        payment_amount_paise=np.array([inr * 100 for _, inr, _ in payments], dtype=np.int64),
        payment_payee=tuple(p for _, _, p in payments),
        payee_added_at_us={PAYEE: _at(days=200), OTHER: _at(days=3)},
        device_first_seen_us={"dev": _at(days=2)},
        login_at_us=np.array([_at(days=9), _at(days=1), _at(minutes=2)], dtype=np.int64),
        login_city=("Hyderabad", "Hyderabad", "Delhi"),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_every_feature_on_a_hand_built_history(config: FeatureConfig) -> None:
    f = build_payment_features(_payment(), _history(), config)
    assert list(f) == list(payment_feature_names(config))
    assert f["amount_log10"] == pytest.approx(4.0)
    in_30d = np.array([1_000, 3_000, 500, 100], dtype=float)
    assert f["amount_z_30d"] == pytest.approx((10_000 - in_30d.mean()) / in_30d.std())
    assert f["amount_ratio_max_lifetime"] == pytest.approx(10_000 / 3_000)
    assert f["hour_sin"] == pytest.approx(math.sin(math.pi)) and f["hour_cos"] == pytest.approx(-1)
    # Earlier local hours: 12:00, 12:00, 12:00, 00:00 and 11:55 → median 12:00, distance 0.
    assert f["hour_deviation"] == pytest.approx(0.0, abs=1e-9)
    assert f["is_new_payee"] == 0.0 and f["payee_age_days"] == pytest.approx(200)
    assert f["payee_account_age_days"] == pytest.approx(400)
    assert f["payee_p_mule"] == 0.1
    assert f["device_age_days"] == pytest.approx(2) and f["is_new_device"] == 1.0
    assert (f["count_10m"], f["count_24h"]) == (1.0, 2.0)
    assert f["sum_24h_ratio_30d"] == pytest.approx(600 / max(4_600 / 30, 100))
    assert f["distinct_payees_24h"] == 2.0
    assert f["new_payees_7d"] == 1.0  # OTHER, saved 3 days ago
    assert f["limit_change_24h"] == 0.0
    assert f["login_new_city"] == 1.0  # Delhi, never seen before
    assert (f["age_years"], f["is_vulnerable"]) == (72.0, 1.0)
    assert f["channel_IMPS"] == 1.0
    assert sum(f[f"channel_{c}"] for c in config.channels) == 1.0
    assert f["session_duration_s"] == 42.0 and f["remote_access_active"] == 0.0


def test_a_first_time_customer(config: FeatureConfig) -> None:
    f = build_payment_features(_payment(), CustomerHistory("c", 30, False), config)
    assert (f["amount_z_30d"], f["hour_deviation"], f["amount_ratio_max_lifetime"]) == (0, 0, 1)
    assert (f["count_24h"], f["sum_24h_ratio_30d"], f["distinct_payees_24h"]) == (0, 0, 0)
    assert f["is_new_payee"] == 1.0 and f["payee_age_days"] == 0.0  # never saved
    assert f["device_age_days"] == 0.0 and f["is_new_device"] == 1.0  # never seen
    assert f["login_new_city"] == 0.0  # no login before: not a new city (TD-8)


@pytest.mark.parametrize(
    ("added_days_ago", "paid_before", "new"),
    [(0.5, True, True), (5, False, True), (5, True, False), (45, False, False)],
)
def test_is_new_payee_follows_the_amended_rule(
    config: FeatureConfig, added_days_ago: float, paid_before: bool, new: bool
) -> None:
    """docs/13 §3: saved < 24 h ago, or never paid and saved within the 30-day window."""
    history = _history(
        payee_added_at_us={PAYEE: _at(days=added_days_ago)},
        payment_at_us=np.array([_at(hours=1)] if paid_before else [], dtype=np.int64),
        payment_amount_paise=np.array([100] if paid_before else [], dtype=np.int64),
        payment_payee=(PAYEE,) if paid_before else (),
    )
    assert build_payment_features(_payment(), history, config)["is_new_payee"] == float(new)


def test_caps_hours_and_channels(config: FeatureConfig) -> None:
    history = _history(device_first_seen_us={"dev": _at(days=4_000)}, payee_added_at_us={})
    payment = _payment(
        payee_account_opened_at_us=_at(days=9_000),
        channel="SOMETHING_ELSE",
        initiated_at_us=to_us(datetime(2026, 7, 1, 0, 30, tzinfo=UTC)),  # 06:00 IST
        session_duration_s=None,
        remote_access_active=True,
    )
    f = build_payment_features(payment, history, config)
    assert f["device_age_days"] == config.device_age_cap_days and f["is_new_device"] == 0.0
    assert f["payee_account_age_days"] == config.payee_account_age_cap_days
    assert sum(f[f"channel_{c}"] for c in config.channels) == 0.0
    assert f["hour_sin"] == pytest.approx(1.0) and f["hour_cos"] == pytest.approx(0, abs=1e-12)
    assert f["session_duration_s"] == 0.0 and f["remote_access_active"] == 1.0


def test_hour_deviation_is_circular(config: FeatureConfig) -> None:
    late = to_us(datetime(2026, 6, 30, 18, 0, tzinfo=UTC))  # 23:30 IST
    history = _history(
        payment_at_us=np.array([late], dtype=np.int64),
        payment_amount_paise=np.array([100], dtype=np.int64),
        payment_payee=(OTHER,),
    )
    early = to_us(datetime(2026, 7, 1, 19, 0, tzinfo=UTC))  # 00:30 IST the next day
    f = build_payment_features(_payment(initiated_at_us=early), history, config)
    assert f["hour_deviation"] == pytest.approx(1.0)


def test_hour_deviation_uses_a_circular_median(config: FeatureConfig) -> None:
    """A customer who pays around midnight: the linear median of [23.5, 23.7, 0.5, 0.6] is
    about 12, which would make a 23:40 payment look 12 hours off (#140 review)."""
    utc = [  # IST is UTC+05:30
        datetime(2026, 6, 27, 18, 0, tzinfo=UTC),  # 23:30 IST
        datetime(2026, 6, 28, 18, 12, tzinfo=UTC),  # 23:42 IST
        datetime(2026, 6, 29, 19, 0, tzinfo=UTC),  # 00:30 IST
        datetime(2026, 6, 30, 19, 6, tzinfo=UTC),  # 00:36 IST
    ]
    history = _history(
        payment_at_us=np.array([to_us(t) for t in utc], dtype=np.int64),
        payment_amount_paise=np.array([100] * 4, dtype=np.int64),
        payment_payee=(OTHER,) * 4,
    )
    at = to_us(datetime(2026, 7, 1, 18, 10, tzinfo=UTC))  # 23:40 IST
    f = build_payment_features(_payment(initiated_at_us=at), history, config)
    # The circular median is about 00:06; 23:40 is about 0.43 h from it.
    assert f["hour_deviation"] == pytest.approx(0.1 + 24 - (23 + 40 / 60), abs=1e-6)


@pytest.mark.parametrize(
    ("hours", "median"),
    [
        ([5.0], 5.0),
        ([1.0, 2.0, 3.0], 2.0),
        ([23.5, 23.7, 0.5, 0.6], 0.1),  # cut at the empty afternoon, not at midnight
        ([22.0, 23.0, 1.0], 23.0),
        ([12.0, 12.0, 12.0], 12.0),
    ],
)
def test_circular_median(hours: list[float], median: float) -> None:
    assert circular_median(np.array(hours)) == pytest.approx(median)


def test_a_device_of_another_customer_is_new_to_this_one(config: FeatureConfig) -> None:
    """The snapshot holds only the customer's own devices, so a fraudster's phone the bank has
    known for months is new to the victim (docs/13 §3, the #140 review)."""
    f = build_payment_features(_payment(device_id="fraudster-phone"), _history(), config)
    assert f["device_age_days"] == 0.0 and f["is_new_device"] == 1.0


def test_paid_before_is_answered_from_the_first_payment_index(config: FeatureConfig) -> None:
    history = _history()
    assert history.first_payment_index == {OTHER: 0, PAYEE: 1}
    # PAYEE was saved 200 days ago and paid 20 days ago: not new.
    assert build_payment_features(_payment(), history, config)["is_new_payee"] == 0.0
    # Saved 10 days ago and never paid before as_of: new (docs/13 §3, the 30-day rule).
    unpaid = _history(payee_added_at_us={PAYEE: _at(days=10), OTHER: _at(days=3)})
    unpaid = replace(unpaid, payment_payee=(OTHER,) * 5)
    assert build_payment_features(_payment(), unpaid, config)["is_new_payee"] == 1.0


def test_a_limit_increase_counts_for_a_day(config: FeatureConfig) -> None:
    limited = config.model_copy(update={"limit_increase_event_types": ("LIMIT_INCREASE",)})
    for hours, expected in ((3, 1.0), (30, 0.0)):
        history = _history(limit_increase_at_us=np.array([_at(hours=hours)], dtype=np.int64))
        assert build_payment_features(_payment(), history, limited)["limit_change_24h"] == expected


def test_events_after_as_of_are_ignored(config: FeatureConfig) -> None:
    """The leakage guard (docs/13 §2): a later payment, payee, device, login or limit change
    changes nothing."""
    history = _history()
    before = build_payment_features(_payment(), history, config)
    later = to_us(AS_OF + timedelta(minutes=1))
    future = replace(
        history,
        payment_at_us=np.append(history.payment_at_us, [to_us(AS_OF), later]),
        payment_amount_paise=np.append(history.payment_amount_paise, [99_900_00, 99_900_00]),
        payment_payee=(*history.payment_payee, "x", "y"),
        payee_added_at_us={**history.payee_added_at_us, "z": later, PAYEE: later},
        device_first_seen_us={"dev": later},
        login_at_us=np.append(history.login_at_us, later),
        login_city=(*history.login_city, "Mumbai"),
        limit_increase_at_us=np.array([later], dtype=np.int64),
    )
    after = build_payment_features(
        _payment(), future, config.model_copy(update={"limit_increase_event_types": ("L",)})
    )
    changed = {k for k in before if before[k] != after[k]}
    # The payee and device were moved to after as_of, so they count as not there yet.
    assert changed == {"is_new_payee", "payee_age_days", "device_age_days"}
    assert after["is_new_payee"] == 1.0 and after["device_age_days"] == 0.0
    untouched = replace(future, payee_added_at_us=history.payee_added_at_us)
    untouched = replace(untouched, device_first_seen_us=history.device_first_seen_us)
    assert build_payment_features(_payment(), untouched, config) == before


def test_the_prior_never_sees_a_probe_input(config: FeatureConfig) -> None:
    """The double-count guard (docs/13 §3)."""
    prior = set(prior_feature_names(config))
    assert not prior & PROBE_INPUT_FEATURES
    assert set(payment_feature_names(config)) >= PROBE_INPUT_FEATURES
    assert {"payee_account_age_days", "login_new_city"} <= prior


def test_new_device_days_is_the_kbs(config: FeatureConfig, kb: KnowledgeBase) -> None:
    assert config.new_device_days == kb.config.classifiers.new_device_days


def test_times_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_us(datetime(2026, 7, 1))
