"""Login events before customer sessions (docs/05 §1, docs/14 §4; #17).

A device always logs in from the same /24, derived from its fingerprint, in the customer's home
city, so `login_new_city` and impossible-travel evidence (docs/13 §3, docs/09 §4) stay quiet for
genuine customers. Fraud scenarios (ATO) log in from elsewhere.
"""

from __future__ import annotations

import functools
import hashlib
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import numpy as np

from delphi.sim.generator_config import City, Range

# Private 10.0.0.0/8: synthetic, never a real network.
_PRIVATE_FIRST_OCTET = 10


@dataclass(frozen=True, slots=True)
class LoginEvent:
    id: UUID
    customer_id: UUID
    device_id: UUID
    ip_prefix24: str
    lat: float
    lon: float
    city: str
    at: datetime


@functools.cache  # one string per device, shared by its logins (#58)
def device_prefix24(fingerprint: str) -> str:
    """A stable synthetic /24 for a device: 10.x.y.0/24 from its fingerprint."""
    digest = hashlib.sha256(fingerprint.encode()).digest()
    network = ipaddress.IPv4Network((f"{_PRIVATE_FIRST_OCTET}.{digest[0]}.{digest[1]}.0", 24))
    return str(network)


def login_before(
    rng: np.random.Generator,
    *,
    login_id: UUID,
    customer_id: UUID,
    device_id: UUID,
    fingerprint: str,
    city_name: str,
    city: City,
    session_at: datetime,
    lead_s: Range,
    not_before: datetime,
) -> LoginEvent:
    """The login ``lead_s`` before the session, but never before ``not_before`` (the start
    of the history)."""
    lead = lead_s.min + (lead_s.max - lead_s.min) * rng.random()
    return LoginEvent(
        id=login_id,
        customer_id=customer_id,
        device_id=device_id,
        ip_prefix24=device_prefix24(fingerprint),
        lat=city.lat,
        lon=city.lon,
        city=city_name,
        at=max(session_at - timedelta(seconds=int(lead)), not_before),
    )
