"""Structured JSON logging.

Components log an event name as the message and attach context through
``extra``::

    log.info("fetch.completed", extra={"url": url, "status": 200})

Each record is emitted as one JSON object per line so crawl logs can be
filtered by event and field without parsing free text.

``log_context(job_id=..., crawl_id=...)`` adds correlation fields (job,
crawl, worker, stage, url, domain) to every record logged inside it,
including from tasks it starts and from ``asyncio.to_thread`` (both copy the
context).

Logs are hardened against untrusted content and credential leaks (spec 28, 31):

* One record is one line: JSON escapes control characters, and NEL and the
  Unicode line separators some viewers break on are escaped too, so page content in
  a field cannot forge extra log lines.
* Credentials are redacted from the final line: the configured secrets
  (``configure_logging(..., secrets=...)``), user info in URLs
  (``scheme://***:***@host``, ``scheme://***@host``) and bearer tokens.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from .config import LoggingSettings

_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("gtm_log_context", default=None)

_URL_CREDENTIALS = re.compile(r"(?<=://)[^/\s:@\"']+:[^@\s/\"']+@")
_URL_USER = re.compile(r"(?<=://)[^/\s:@\"'*]+@")  # a token passed as the user name
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
REDACTED = "***"
# Line breaks JSON leaves raw (it escapes only U+0000-U+001F) but that
# str.splitlines and some log viewers split on.
_LINE_BREAKS = [(c, f"\\u{ord(c):04x}") for c in "\x85\u2028\u2029"]


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach ``fields`` to every record logged in this context."""
    token = _CONTEXT.set({**(_CONTEXT.get() or {}), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


# Attributes every LogRecord has; anything else on a record came from ``extra``.
_RESERVED = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        # Longest first, so a secret containing another is redacted whole.
        # Each also in its JSON-escaped form, as it appears inside the line.
        values = {s for s in secrets if s}
        values |= {json.dumps(s, ensure_ascii=False)[1:-1] for s in values}
        self._secrets = sorted(values, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **(_CONTEXT.get() or {}),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        line = json.dumps(entry, default=str, ensure_ascii=False)
        for char, escaped in _LINE_BREAKS:
            line = line.replace(char, escaped)
        return self.redact(line)

    def redact(self, line: str) -> str:
        for secret in self._secrets:
            line = line.replace(secret, REDACTED)
        line = _URL_CREDENTIALS.sub(f"{REDACTED}:{REDACTED}@", line)
        line = _URL_USER.sub(f"{REDACTED}@", line)
        return _BEARER.sub(f"Bearer {REDACTED}", line)


def configure_logging(
    settings: LoggingSettings, stream: Any = None, secrets: Iterable[str] = ()
) -> None:
    """Route the ``gtm_crawler`` logger tree to a JSON handler that redacts ``secrets``."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter(secrets))
    root = logging.getLogger("gtm_crawler")
    root.handlers[:] = [handler]
    root.setLevel(settings.level.upper())
    root.propagate = False
    from . import tracing  # imports this module

    tracing.SAMPLE_RATE = settings.trace_sample_rate
