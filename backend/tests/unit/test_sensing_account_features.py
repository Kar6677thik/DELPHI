"""The account features (docs/13 §6) the mule scorer reads, on hand-built histories."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from delphi.sensing.config import AccountFeatureConfig, load_sensing_config
from delphi.sensing.features import (
    ACCOUNT_FEATURES,
    AccountHistory,
    build_account_features,
    to_us,
)

AS_OF = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def config() -> AccountFeatureConfig:
    return load_sensing_config().accounts


def _at(**delta: float) -> int:
    return to_us(AS_OF - timedelta(**delta))


def _ints(*values: int) -> np.ndarray:  # type: ignore[type-arg]
    return np.array(values, dtype=np.int64)


def _mule() -> AccountHistory:
    """A layer-1 mule: three credits in two days, each forwarded within minutes, half cashed
    out, from a device two other accounts used too, named in one complaint."""
    return AccountHistory(
        account_id="mule",
        opened_at_us=_at(days=20),
        kyc_level="MINIMAL",
        inbound_at_us=_ints(_at(days=10), _at(hours=40), _at(hours=20), _at(hours=2)),
        inbound_amount_paise=_ints(1_000_00, 50_000_00, 30_000_00, 20_000_00),
        inbound_payer=("v0", "v1", "v2", "v1"),
        outbound_at_us=_ints(_at(hours=39, minutes=50), _at(hours=19), _at(hours=1)),
        outbound_amount_paise=_ints(45_000_00, 30_000_00, 20_000_00),
        outbound_channel=("IMPS", "CASH_OUT", "IMPS"),
        device_use_at_us=_ints(_at(days=40), _at(hours=39, minutes=50), _at(hours=19)),
        device_use_fingerprint=("old", "shared", None),
        shared_use_at_us=_ints(_at(days=45), _at(days=3), _at(days=2), _at(days=1)),
        shared_use_fingerprint=("shared", "shared", "shared", "old"),
        shared_use_account=("m9", "m2", "m3", "m4"),
        complaint_at_us=_ints(_at(hours=5)),
    )


def test_every_account_feature_on_a_mule(config: AccountFeatureConfig) -> None:
    f = build_account_features(_mule(), to_us(AS_OF), config)
    assert list(f) == list(ACCOUNT_FEATURES)
    assert f["inbound_distinct_payers_48h"] == 2.0  # v1 twice, v2
    assert f["inbound_count_7d"] == 3.0
    assert f["inbound_sum_7d_log"] == pytest.approx(math.log10(1 + 100_000))
    # Dwell: 10 min, 60 min and 60 min to the next debit → median 60.
    assert f["median_dwell_minutes_7d"] == pytest.approx(60.0)
    assert f["outbound_to_inbound_ratio_7d"] == pytest.approx(95_000 / 100_000)
    assert f["account_age_days"] == pytest.approx(20.0)
    assert f["kyc_minimal"] == 1.0
    # "shared" was used in 30 days; m2 and m3 used it in 30 days; m9 too long ago; "old" was
    # only used by this account 40 days ago, so m4 does not count.
    assert f["device_shared_accounts_30d"] == 2.0
    assert f["cash_out_share_7d"] == pytest.approx(30_000 / 95_000)
    assert f["complaints_linked"] == 1.0


def test_a_quiet_genuine_account(config: AccountFeatureConfig) -> None:
    quiet = AccountHistory(account_id="g", opened_at_us=_at(days=8_000), kyc_level="FULL")
    f = build_account_features(quiet, to_us(AS_OF), config)
    assert f["account_age_days"] == config.account_age_cap_days
    assert f["median_dwell_minutes_7d"] == config.flow_days * 24 * 60  # nothing left: the window
    assert (f["inbound_count_7d"], f["inbound_sum_7d_log"], f["kyc_minimal"]) == (0, 0, 0)
    assert (f["outbound_to_inbound_ratio_7d"], f["cash_out_share_7d"]) == (0, 0)
    assert (f["device_shared_accounts_30d"], f["complaints_linked"]) == (0, 0)


def test_a_credit_not_yet_moved_on_is_left_out_of_the_dwell(config: AccountFeatureConfig) -> None:
    history = replace(
        _mule(),
        inbound_at_us=_ints(_at(hours=3), _at(hours=1)),
        inbound_amount_paise=_ints(100, 100),
        inbound_payer=("a", "b"),
        outbound_at_us=_ints(_at(hours=2)),
        outbound_amount_paise=_ints(100),
        outbound_channel=("IMPS",),
    )
    assert build_account_features(history, to_us(AS_OF), config)[
        "median_dwell_minutes_7d"
    ] == pytest.approx(60.0)


def test_events_after_as_of_are_ignored(config: AccountFeatureConfig) -> None:
    mule = _mule()
    before = build_account_features(mule, to_us(AS_OF), config)
    later = to_us(AS_OF + timedelta(seconds=1))
    future = replace(
        mule,
        inbound_at_us=np.append(mule.inbound_at_us, [to_us(AS_OF), later]),
        inbound_amount_paise=np.append(mule.inbound_amount_paise, [9_999_00, 9_999_00]),
        inbound_payer=(*mule.inbound_payer, "x", "y"),
        outbound_at_us=np.append(mule.outbound_at_us, later),
        outbound_amount_paise=np.append(mule.outbound_amount_paise, 9_999_00),
        outbound_channel=(*mule.outbound_channel, "CASH_OUT"),
        device_use_at_us=np.append(mule.device_use_at_us, later),
        device_use_fingerprint=(*mule.device_use_fingerprint, "old"),
        shared_use_at_us=np.append(mule.shared_use_at_us, later),
        shared_use_fingerprint=(*mule.shared_use_fingerprint, "shared"),
        shared_use_account=(*mule.shared_use_account, "m7"),
        complaint_at_us=np.append(mule.complaint_at_us, later),
    )
    assert build_account_features(future, to_us(AS_OF), config) == before
