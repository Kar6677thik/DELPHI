"""Server-side sessions and the login throttle (docs/19 §2, docs/16 §1).

A session is a random 256-bit token held by the browser in the `delphi_session` cookie. Only its
SHA-256 is stored (`auth_sessions.id`), so a database leak does not hand out live sessions. A
session ends after 12 h without use (idle) or 7 days after sign-in (absolute), whichever comes
first. Signing in always issues a new token and ends the session the browser presented, so a
token planted before login is useless after it (session fixation).
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from delphi.platform.accounts import User, get_user
from delphi.platform.clock import Clock
from delphi.platform.tables import auth_sessions

TOKEN_BYTES = 32  # 256 bits
# `last_seen_at` is written at most this often, not on every request.
LAST_SEEN_RESOLUTION = timedelta(minutes=1)
MAX_USER_AGENT = 512  # the column's CHECK (migration 0001)


def token_id(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    idle: timedelta = timedelta(hours=12)
    absolute: timedelta = timedelta(days=7)


def _valid_ip(ip: str | None) -> str | None:
    """The address if it parses, else None (the column is `inet`; a bad header must not break
    sign-in)."""
    if not ip:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def start_session(
    session: Session,
    clock: Clock,
    policy: SessionPolicy,
    user: User,
    *,
    ip: str | None,
    user_agent: str | None,
    replaces: str | None = None,
) -> str:
    """Create a session for ``user`` and return its token. ``replaces`` is the token the
    browser sent with the login request, if any: that session ends now."""
    if replaces:
        session.execute(delete(auth_sessions).where(auth_sessions.c.id == token_id(replaces)))
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = clock.now()
    session.execute(
        auth_sessions.insert().values(
            id=token_id(token),
            user_id=user.id,
            created_at=now,
            expires_at=now + policy.absolute,
            last_seen_at=now,
            ip=_valid_ip(ip),
            user_agent=(user_agent or "")[:MAX_USER_AGENT] or None,
        )
    )
    return token


def resolve_session(
    session: Session, clock: Clock, policy: SessionPolicy, token: str
) -> User | None:
    """The signed-in, enabled user for ``token``, or None. An expired session is deleted."""
    key = token_id(token)
    row = session.execute(select(auth_sessions).where(auth_sessions.c.id == key)).one_or_none()
    if row is None:
        return None
    now = clock.now()
    if now >= row.expires_at or now - row.last_seen_at >= policy.idle:
        session.execute(delete(auth_sessions).where(auth_sessions.c.id == key))
        return None
    user = get_user(session, row.user_id)
    if user is None or user.disabled:
        return None
    if now - row.last_seen_at >= LAST_SEEN_RESOLUTION:
        session.execute(
            update(auth_sessions).where(auth_sessions.c.id == key).values(last_seen_at=now)
        )
    return user


def end_session(session: Session, token: str) -> None:
    session.execute(delete(auth_sessions).where(auth_sessions.c.id == token_id(token)))


def end_user_sessions(session: Session, user_id: object) -> None:
    """Sign a user out everywhere (on disable, password reset or role change)."""
    session.execute(delete(auth_sessions).where(auth_sessions.c.user_id == user_id))


@dataclass(slots=True)
class LoginThrottle:
    """At most ``max_failures`` failed logins per (username, IP) in ``window`` (docs/16 §1).

    Kept in process memory: the api runs one replica (docs/23 §4), and a restart forgetting
    recent failures is acceptable for a demo. Cloudflare's rate-limit rule on /auth/login is a
    second layer (docs/23 §6).
    """

    clock: Clock
    max_failures: int = 5
    window: timedelta = timedelta(minutes=15)
    _failures: dict[tuple[str, str], deque[datetime]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _recent(self, key: tuple[str, str], now: datetime) -> deque[datetime]:
        times = self._failures.setdefault(key, deque())
        while times and now - times[0] >= self.window:
            times.popleft()
        return times

    def retry_after(self, username: str, ip: str) -> timedelta | None:
        """None if a login may be attempted, else how long until it may."""
        now = self.clock.now()
        with self._lock:
            times = self._recent((username, ip), now)
            if len(times) < self.max_failures:
                return None
            return times[0] + self.window - now

    def failed(self, username: str, ip: str) -> None:
        now = self.clock.now()
        with self._lock:
            self._recent((username, ip), now).append(now)

    def succeeded(self, username: str, ip: str) -> None:
        with self._lock:
            self._failures.pop((username, ip), None)
