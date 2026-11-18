"""The pure customer-channel helpers (docs/08 §4–5, #94 part 3)."""

from __future__ import annotations

import pytest

from delphi.guardian.active import (
    ClientSignals,
    Ui,
    biometric_observation,
    cooling_off_observation,
    new_otp,
    otp_code,
    otp_digest,
    otp_matches,
    question_observation,
    response_latency_s,
    timed_out_observation,
    ui_for,
)
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.probes.api import BiometricObs, CoolingOffObs, OtpObs


def test_each_active_probe_has_a_screen() -> None:
    assert ui_for("PURPOSE_QUESTION", question=True) is Ui.QUESTION
    assert ui_for("OTP_STEP_UP", question=False) is Ui.OTP
    assert ui_for("BIOMETRIC_STEP_UP", question=False) is Ui.BIOMETRIC
    assert ui_for("COOLING_OFF", question=False) is Ui.COOLING_OFF
    with pytest.raises(ValueError, match="no customer screen"):
        ui_for("CALL_STATUS", question=False)


def test_otp_codes_are_six_digits_and_only_their_keyed_digest_matches() -> None:
    codes = {new_otp() for _ in range(50)}
    assert all(len(c) == 6 and c.isdigit() for c in codes) and len(codes) > 1
    digest = otp_digest("run-1", "123456")
    assert otp_matches("run-1", "123456", digest)
    assert not otp_matches("run-1", "123457", digest)
    assert not otp_matches("run-2", "123456", digest)  # keyed by the probe run
    for bad in ({"otp": "12345"}, {"otp": "12345a"}, {"otp": 123456}, {}):
        with pytest.raises(DelphiError, match="6 digits"):
            otp_code(bad)


@pytest.mark.parametrize(
    ("shown", "answered", "server_s", "ok"),
    [
        (0, 4_000, 4.0, True),
        (0, 14_000, 4.0, True),  # 10 s of slack (docs/08 §4)
        (0, 14_001, 4.0, False),
        (5, 1, 4.0, False),  # negative
    ],
)
def test_client_latency_is_checked_against_what_the_server_saw(
    shown: int, answered: int, server_s: float, ok: bool
) -> None:
    signals = ClientSignals(shown, answered)
    if ok:
        assert response_latency_s(signals, server_s) == (answered - shown) / 1000
    else:
        with pytest.raises(DelphiError) as err:
            response_latency_s(signals, server_s)
        assert err.value.code is ProblemCode.INVALID_CLIENT_SIGNALS


def test_negative_counts_are_implausible() -> None:
    with pytest.raises(DelphiError) as err:
        response_latency_s(ClientSignals(0, 1_000, visibility_hidden_count=-1), 1.0)
    assert err.value.code is ProblemCode.INVALID_CLIENT_SIGNALS


def test_answers_become_observations() -> None:
    signals = ClientSignals(0, 3_000, visibility_hidden_count=2)
    obs = question_observation({"option_key": "YES"}, frozenset({"YES", "NO"}), signals, 3.0)
    assert (obs.option, obs.latency_s, obs.app_switch_count) == ("YES", 3.0, 2)
    with pytest.raises(DelphiError, match="options shown"):
        question_observation({"option_key": "MAYBE"}, frozenset({"YES", "NO"}), signals, 3.0)
    assert biometric_observation({"biometric": "MATCH"}) == BiometricObs("MATCH")
    with pytest.raises(DelphiError, match="biometric"):
        biometric_observation({"biometric": "TIMEOUT"})  # only the server times out
    assert cooling_off_observation(
        {"cooling_off": {"cancelled": False, "send_now_presses": 2}}
    ) == CoolingOffObs(False, 0, 0, 2)
    for bad in (
        {},
        {"cooling_off": {}},
        {"cooling_off": {"cancelled": False, "send_now_presses": -1}},
    ):
        with pytest.raises(DelphiError):
            cooling_off_observation(bad)


def test_a_timed_out_step_up_is_a_failed_observation() -> None:
    assert timed_out_observation("OTP_STEP_UP", 1) == OtpObs(False, 1, True)
    assert timed_out_observation("BIOMETRIC_STEP_UP", 0) == BiometricObs("TIMEOUT")
