"""Structured JSON logging (docs/20 §1).

One JSON object per line on stdout. Context (request id, user, session, case, job) is bound
with ``bind_context`` and carried by contextvars, so it follows the request or job through
``await`` points. Values whose key looks like a secret are masked before they are written, and
so are secrets inside any string, the formatted exception included (#127): a URL's password
(``postgresql://user:***@host``) and an Anthropic key.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, TextIO

import structlog

REDACTED = "***"
_SECRET_KEY = re.compile(r"(?i)(password|token|secret|api_key|authorization|cookie)")
# Secrets that turn up inside text: a URL's password, an Anthropic API key.
_URL_PASSWORD = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^:/@\s]*:)[^@\s]+@")
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]+")

bind_context = structlog.contextvars.bind_contextvars
clear_context = structlog.contextvars.clear_contextvars


def scrub(text: str) -> str:
    """Mask the secrets ``text`` may contain."""
    return _ANTHROPIC_KEY.sub(REDACTED, _URL_PASSWORD.sub(rf"\g<1>{REDACTED}@", text))


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, Mapping):
        return {
            k: REDACTED if _SECRET_KEY.search(str(k)) else _redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Mask every key (at any depth) matching the docs/20 §1 pattern."""
    for key in list(event_dict):
        if _SECRET_KEY.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def scrub_exception(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """The traceback is formatted after ``redact_secrets``, so scrub it once it exists."""
    if isinstance(event_dict.get("exception"), str):
        event_dict["exception"] = scrub(event_dict["exception"])
    return event_dict


def configure_logging(level: str, stream: TextIO | None = None) -> None:
    """Route both structlog and stdlib logging (uvicorn, alembic, sqlalchemy) to JSON lines."""
    out = stream or sys.stdout
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        redact_secrets,
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            scrub_exception,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(out)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # One structured request line per request replaces uvicorn's access log (docs/20 §1).
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


def get_logger(name: str) -> Any:
    return structlog.stdlib.get_logger(name)
