"""Exceptions raised by the client. All inherit from :class:`LMSError`."""

from __future__ import annotations

from typing import Any


class LMSError(Exception):
    """Base class. ``status_code`` is ``None`` for errors that never reached the server."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: dict[str, Any] | None = None,
        *,
        error_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body or {}
        self.error_code = error_code or self.response_body.get("error_code")
        self.request_id = request_id or self.response_body.get("request_id")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status_code={self.status_code}, "
            f"error_code={self.error_code!r}, message={self.message!r})"
        )


class LMSConfigError(LMSError, ValueError):
    """The client or a log entry is missing required configuration (e.g. source_service)."""


class LMSAuthError(LMSError):
    """401 — missing/invalid signature or the timestamp is outside the server's window."""


class LMSPermissionError(LMSError):
    """403 — e.g. ``SERVICE_DISABLED``: the service was disabled in the LMS."""


class LMSNotFoundError(LMSError):
    """404 — unknown endpoint (check ``base_url``)."""


class LMSConflictError(LMSError):
    """409 — e.g. ``DUPLICATE_REQUEST``: the signed request was already processed."""


class LMSValidationError(LMSError):
    """422 — payload rejected, or ``SERVICE_NOT_REGISTERED``. Details in ``response_body``."""


class LMSRateLimitError(LMSError):
    """429 — per-service rate limit. ``retry_after`` is in seconds."""

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class LMSServerError(LMSError):
    """5xx — the LMS failed; retried automatically before this is raised."""


class LMSConnectionError(LMSError):
    """The LMS could not be reached (DNS, TCP, TLS, timeout) after all retries."""


class LMSQueueFullError(LMSError):
    """Buffered mode: the in-memory queue is full and the entry was dropped."""
