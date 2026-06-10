"""Logging configured for systemd / journalctl.

- Logs go to stdout/stderr (journald captures them; no file handling needed).
- "json" format emits one JSON object per line -> easy to grep/parse in journald.
- A redaction filter is attached so no log record can leak a secret, even if a
  caller forgets to scrub it.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .secretsafe import _redact_string


class _RedactionFilter(logging.Filter):
    """Last line of defence: scrub secret-looking substrings from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = _redact_string(msg)
        record.args = ()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear any pre-existing handlers (idempotent across run_once invocations).
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    handler.addFilter(_RedactionFilter())
    root.addHandler(handler)

    # Quiet noisy third-party loggers.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
