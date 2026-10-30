"""Bridge from the standard :mod:`logging` module to the LMS."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .models import LogLevel

if TYPE_CHECKING:
    from .client import LMSClient

# Attributes every LogRecord has; anything else came from `extra=` and is forwarded.
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
# Records from the HTTP stack (and ourselves) must never be shipped: sending them
# would log again and loop forever.
_IGNORED_PREFIXES = ("lms_logger", "httpx", "httpcore", "h11", "h2", "hpack")


def level_for(levelno: int) -> LogLevel:
    if levelno >= logging.CRITICAL:
        return LogLevel.FATAL
    if levelno >= logging.ERROR:
        return LogLevel.ERROR
    if levelno >= logging.WARNING:
        return LogLevel.WARN
    if levelno >= logging.INFO:
        return LogLevel.INFO
    return LogLevel.DEBUG


class LMSHandler(logging.Handler):
    """Forward log records to the LMS.

    Use a *buffered* client so ``emit`` never blocks on the network::

        lms = LMSClient(..., buffered=True)
        logging.getLogger().addHandler(lms.handler(level=logging.INFO))

    The record's message becomes the log message; ``logger``, ``module``,
    ``function``, ``line``, the formatted traceback (if any) and every ``extra=``
    field go into ``context_json``. Put ``trace_id`` in ``extra`` to set it.
    """

    def __init__(self, client: LMSClient, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.client = client
        if not client.buffered:
            import warnings

            warnings.warn(
                "LMSHandler with an unbuffered client sends one HTTP request per record "
                "in the calling thread; create the client with buffered=True",
                RuntimeWarning,
                stacklevel=3,
            )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(_IGNORED_PREFIXES):
            return False
        return bool(super().filter(record))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            context: dict[str, Any] = {
                "logger": record.name,
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
                "thread": record.threadName,
            }
            if record.exc_info:
                context["exception"] = "".join(traceback.format_exception(*record.exc_info))
            trace_id = None
            for key, value in vars(record).items():
                if key in _STANDARD_ATTRS or key.startswith("_"):
                    continue
                if key == "trace_id":
                    trace_id = str(value) if value is not None else None
                    continue
                context[key] = (
                    value if isinstance(value, (str, int, float, bool, type(None))) else repr(value)
                )
            self.client.log(
                level_for(record.levelno),
                record.getMessage(),
                context,
                trace_id=trace_id,
                created_at=datetime.fromtimestamp(record.created, timezone.utc),
            )
        except Exception:
            self.handleError(record)
