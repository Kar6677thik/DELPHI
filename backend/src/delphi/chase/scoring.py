"""Freeze-candidate scoring (docs/10 §4, FR-CHS-02, FR-CHS-03).

For every account still holding at least ``min_trace_inr`` of taint, the chase asks: if we put a
lien on it now, how much do we expect to recover, and what does a wrongful lien cost?

- ``p_present = exp(−Δ / τ_acc)``: the chance the money is still there when the lien lands,
  ``Δ`` being the freeze latency (internal or external bank) and ``τ_acc`` the account's mean
  dwell time;
- ``ERV = lien · p_present · (p_mule + (1 − p_mule) · κ)``;
- ``wrongful = (1 − p_mule) · (base + rate · lien)``;
- ``net = ERV − wrongful``; ranked by ``net``, recommended when ``net > 0``.

**Dwell time.** From the account's credits in the last 30 days: for each credit, the time until
80% of it has left, taking the account's money out first in, first out (the balance it held
before the window leaves first). ``τ_acc`` is the median of those times ÷ ln 2 (an exponential
dwell whose half-life is the median). A credit not yet 80% out counts with the time it has been
there so far: a lower bound, so an account that keeps its money is never made to look faster
than it is seen to be. With fewer than 3 credits the KB fallback applies: 20 min if
``p_mule ≥ 0.5``, else 24 h. All durations are scaled by the demo time scale.

The module is pure: the caller (the chase operation, #113) loads the moves, the taint and the
mule scores. Money is whole paise; the expected values are rounded to the paisa.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import median

from delphi.kb.model import ChaseSection

PAISE_PER_INR = 100
US_PER_MINUTE = 60_000_000
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60

# The dwell definition of docs/10 §4 (a definition, not a tunable).
DWELL_WINDOW_DAYS = 30
DWELL_OUT_FRACTION = 0.8
DWELL_MIN_CREDITS = 3
MULE_LIKELY = 0.5


@dataclass(frozen=True, slots=True)
class Move:
    """A credit (``delta_paise`` > 0) or debit (< 0) on one account, at ``at_us``."""

    at_us: int
    delta_paise: int


@dataclass(frozen=True, slots=True)
class Candidate:
    """An account holding taint, with what the scorer needs to know about it."""

    account_id: str
    taint_paise: int  # T_now
    balance_paise: int  # the current balance
    p_mule: float
    internal: bool  # held at this bank (a 60 s lien) or another (a 30 min ticket)
    dwell_minutes: float  # τ_acc, already time-scaled


@dataclass(frozen=True, slots=True)
class CandidateScore:
    account_id: str
    lien_paise: int
    p_present: float
    p_mule: float
    erv_paise: int
    wrongful_paise: int
    net_paise: int
    rank: int
    recommended: bool


def dwell_window_start(now_us: int) -> int:
    """The first instant whose credits count towards the dwell time (the last 30 days)."""
    return now_us - DWELL_WINDOW_DAYS * 24 * MINUTES_PER_HOUR * US_PER_MINUTE


def times_to_out(
    opening_paise: int,
    moves: Iterable[Move],
    *,
    since_us: int,
    now_us: int,
    fraction: float = DWELL_OUT_FRACTION,
) -> list[float]:
    """Minutes until ``fraction`` of each credit at or after ``since_us`` left the account.

    ``opening_paise`` is the balance before the first move; it leaves first. A credit not yet
    out by ``now_us`` counts with the minutes it has been there (censored at now).
    """
    # Each lot: [credited_at_us, remaining_paise, amount_paise, index among the counted credits].
    lots: deque[list[int]] = deque()
    if opening_paise > 0:
        lots.append([0, opening_paise, 0, -1])
    credited: list[int] = []  # when each counted credit arrived
    out: dict[int, float] = {}  # counted credit → minutes to `fraction` out
    for move in sorted(moves, key=lambda m: m.at_us):
        if move.delta_paise > 0:
            counted = move.at_us >= since_us
            lots.append(
                [move.at_us, move.delta_paise, move.delta_paise, len(credited) if counted else -1]
            )
            if counted:
                credited.append(move.at_us)
            continue
        left = -move.delta_paise
        while left > 0 and lots:
            lot = lots[0]
            taken = min(left, lot[1])
            lot[1] -= taken
            left -= taken
            index = lot[3]
            if index >= 0 and index not in out and lot[1] <= lot[2] * (1 - fraction):
                out[index] = (move.at_us - lot[0]) / US_PER_MINUTE
            if lot[1] == 0:
                lots.popleft()
    return [out.get(i, (now_us - at) / US_PER_MINUTE) for i, at in enumerate(credited)]


def dwell_minutes(
    times_out_minutes: Sequence[float], p_mule: float, chase: ChaseSection, time_scale: float
) -> float:
    """``τ_acc`` in (scaled) minutes: median time-to-80%-out ÷ ln 2, or the KB fallback."""
    if len(times_out_minutes) >= DWELL_MIN_CREDITS:
        return max(median(times_out_minutes), 0.0) / math.log(2) * time_scale
    fallback = (
        chase.dwell_fallback_mule_min
        if p_mule >= MULE_LIKELY
        else chase.dwell_fallback_other_h * MINUTES_PER_HOUR
    )
    return fallback * time_scale


def freeze_latency_minutes(internal: bool, chase: ChaseSection, time_scale: float) -> float:
    latency = (
        chase.freeze_latency_internal_s / SECONDS_PER_MINUTE
        if internal
        else chase.freeze_latency_external_min
    )
    return latency * time_scale


def score(
    candidates: Iterable[Candidate], chase: ChaseSection, time_scale: float
) -> list[CandidateScore]:
    """Score and rank the freeze candidates (those with T_now ≥ ``min_trace_inr``)."""
    floor = round(chase.min_trace_inr * PAISE_PER_INR)
    scored: list[tuple[str, int, float, float, int, int, int]] = []
    for c in candidates:
        if c.taint_paise < floor:
            continue
        lien = max(min(c.taint_paise, c.balance_paise), 0)
        delta = freeze_latency_minutes(c.internal, chase, time_scale)
        p_present = math.exp(-delta / c.dwell_minutes) if c.dwell_minutes > 0 else 0.0
        upheld = c.p_mule + (1 - c.p_mule) * chase.lawful_hold_kappa
        erv = round(lien * p_present * upheld)
        wrongful = round(
            (1 - c.p_mule) * (chase.wrongful_base_inr * PAISE_PER_INR + chase.wrongful_rate * lien)
        )
        scored.append((c.account_id, lien, p_present, c.p_mule, erv, wrongful, erv - wrongful))
    # By net, then by ERV, then by account id, so the order is stable.
    scored.sort(key=lambda s: (-s[6], -s[4], s[0]))
    return [
        CandidateScore(
            account_id=a,
            lien_paise=lien,
            p_present=p,
            p_mule=m,
            erv_paise=erv,
            wrongful_paise=w,
            net_paise=net,
            rank=i + 1,
            recommended=net > 0,
        )
        for i, (a, lien, p, m, erv, w, net) in enumerate(scored)
    ]
