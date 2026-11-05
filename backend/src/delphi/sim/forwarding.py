"""Money moving down a mule ring (docs/14 §4, #17).

A credit that reaches a mule starts a *flow* there. After a log-normal dwell (median 12 min
at layer 1, 25 min deeper, σ 0.8), the mule forwards it as one onward transfer: 1–3 payments
to one account of the next layer. Once every payment of the transfer is resolved, what arrived
starts the next flow. At the final layer the credit is cashed out (channel ``CASH_OUT``, to the
bank's cash-out account) with probability 0.4, else it stays. Each flow draws from
``keyed_rng(seed, "flow", n)``, numbered in the order flows start, which the history's
chronological pass fixes.

The history makes the payments: a home-bank mule's is an app session (MULE_FORWARDING,
decided by B1); an external mule's, and every cash-out, is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from uuid import UUID

import numpy as np

from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.ids import IdStream
from delphi.sim.mules import MuleNetwork
from delphi.sim.population import ACTIVE, FULL_KYC, HOME_BANK, Account, _choice
from delphi.sim.rng import keyed_rng

CASH_OUT = "CASH_OUT"
_PAISE_PER_INR = 100
_CASH_ACCOUNT_AGE_DAYS = 3650


@dataclass(frozen=True, slots=True)
class Hop:
    """One payment of an onward transfer or a cash-out."""

    flow: int
    payer: UUID
    payee: UUID
    amount_paise: int
    at: datetime
    channel: str
    session: bool  # a home-bank mule's app session


@dataclass(slots=True)
class _Flow:
    to: UUID
    pending: int
    received: int = 0


def _split(rng: np.random.Generator, amount_paise: int, parts: int) -> list[int]:
    """``parts`` whole-rupee amounts that add up to ``amount_paise`` (the last takes the rest)."""
    rupees = amount_paise // _PAISE_PER_INR
    parts = max(1, min(parts, rupees))
    cuts = sorted(int(rng.integers(1, rupees)) for _ in range(parts - 1)) if parts > 1 else []
    edges = [0, *cuts, rupees]
    shares = [(b - a) * _PAISE_PER_INR for a, b in pairwise(edges)]
    shares[-1] += amount_paise - rupees * _PAISE_PER_INR
    return [s for s in shares if s > 0]


class Forwarder:
    def __init__(
        self, config: GeneratorConfig, network: MuleNetwork, *, seed: int, start: datetime
    ):
        self.cfg = config.forwarding
        self.seed = seed
        self.layer_of = {
            account: (ring, layer)
            for ring in network.rings
            for layer, accounts in enumerate(ring.layers)
            for account in accounts
        }
        self.bank_of = {a.id: a.bank_code for a in network.accounts}
        ids = IdStream(seed, "cash_out_account")
        self.cash_accounts = {
            bank: Account(
                id=ids.next(),
                customer_id=None,
                bank_code=bank,
                is_merchant=False,
                merchant_category=None,
                opened_at=start - timedelta(days=_CASH_ACCOUNT_AGE_DAYS),
                kyc_level=FULL_KYC,
                status=ACTIVE,
                balance_paise=0,
                phone_number=None,
                vpa=None,
            )
            for bank in (HOME_BANK, *config.population.external_banks)
        }
        self.flows: list[_Flow | None] = []

    def is_mule(self, account: UUID) -> bool:
        return account in self.layer_of

    def arrive(self, account: UUID, amount_paise: int, at: datetime) -> list[Hop]:
        """A credit reaches a mule: the payments that will move it on."""
        flow = len(self.flows)
        rng = keyed_rng(self.seed, "flow", str(flow))
        ring, layer = self.layer_of[account]
        dwell = self.cfg.first_layer_dwell_minutes if layer == 0 else self.cfg.deeper_dwell_minutes
        at += timedelta(minutes=dwell.median * math.exp(dwell.sigma * rng.standard_normal()))
        if layer == len(ring.layers) - 1:
            if rng.random() >= self.cfg.cash_out_share:
                self.flows.append(None)  # the money stays
                return []
            sink = self.cash_accounts[self.bank_of[account]].id
            self.flows.append(_Flow(sink, 1))
            return [Hop(flow, account, sink, amount_paise, at, CASH_OUT, session=False)]
        following = ring.layers[layer + 1]
        target = following[int(rng.integers(len(following)))]
        parts = int(rng.integers(int(self.cfg.splits.min), int(self.cfg.splits.max) + 1))
        session = self.bank_of[account] == HOME_BANK
        hops = []
        for amount in _split(rng, amount_paise, parts):
            channel = _choice(rng, self.cfg.channel)
            hops.append(Hop(flow, account, target, amount, at, channel, session))
            gap = self.cfg.split_gap_minutes
            at += timedelta(minutes=gap.min + (gap.max - gap.min) * rng.random())
        self.flows.append(_Flow(target, len(hops)))
        return hops

    def resolve(self, flow: int, executed_paise: int, at: datetime) -> list[Hop]:
        """A payment of the flow is done (``executed_paise`` = 0 if held, declined or never
        made). After the last one, what arrived moves on from the next mule."""
        state = self.flows[flow]
        if state is None:
            return []
        state.pending -= 1
        state.received += executed_paise
        if state.pending or not state.received or not self.is_mule(state.to):
            return []
        return self.arrive(state.to, state.received, at)
