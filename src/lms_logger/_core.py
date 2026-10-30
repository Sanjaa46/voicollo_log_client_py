"""Transport-independent pieces shared by the sync and async clients."""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from ._version import __version__
from .exceptions import (
    LMSAuthError,
    LMSConfigError,
    LMSConflictError,
    LMSError,
    LMSNotFoundError,
    LMSPermissionError,
    LMSRateLimitError,
    LMSServerError,
    LMSValidationError,
)
from .models import AppLogRequest, AuditLogRequest, LogLevel
from .signing import signed_headers

APP_PATH = "/api/v1/logs/app"
BATCH_PATH = "/api/v1/logs/app/batch"
AUDIT_PATH = "/api/v1/logs/audit"
MAX_BATCH = 100
USER_AGENT = f"lms-logger-python/{__version__}"

ErrorHandler = Callable[[Exception, Any], None]
"""``on_error(exc, payload)`` — called for failures in buffered mode."""

_STATUS_ERRORS: dict[int, type[LMSError]] = {
    401: LMSAuthError,
    403: LMSPermissionError,
    404: LMSNotFoundError,
    409: LMSConflictError,
    422: LMSValidationError,
    429: LMSRateLimitError,
}
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

M = TypeVar("M", bound=BaseModel)


@dataclass
class ClientConfig:
    base_url: str
    secret: str
    source_service: str | None = None
    environment: str | None = None
    timeout: float = 10.0
    max_retries: int = 3
    backoff: float = 0.5
    max_backoff: float = 30.0
    buffered: bool = False
    batch_size: int = 50
    flush_interval: float = 2.0
    max_queue: int = 10_000
    on_error: ErrorHandler | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.base_url:
            raise LMSConfigError("base_url is required")
        if not self.secret:
            raise LMSConfigError("secret is required (the LMS INGEST_SIGNING_SECRET)")
        self.base_url = self.base_url.rstrip("/")
        if self.max_retries < 0:
            raise LMSConfigError("max_retries must be >= 0")
        if not 1 <= self.batch_size <= MAX_BATCH:
            raise LMSConfigError(f"batch_size must be between 1 and {MAX_BATCH}")


class RequestSigner:
    """Signs each attempt with a fresh timestamp that never repeats for this client,
    so a retry can never be mistaken for a replay by the server."""

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._last = 0

    def headers(self, body: bytes) -> dict[str, str]:
        ts = max(int(time.time()), self._last + 1)
        self._last = ts
        return signed_headers(self._secret, body, str(ts))


def apply_defaults(entry: M, config: ClientConfig) -> M:
    updates: dict[str, Any] = {}
    if getattr(entry, "source_service", None) is None:
        updates["source_service"] = config.source_service
    if getattr(entry, "environment", None) is None:
        updates["environment"] = config.environment
    if updates:
        entry = entry.model_copy(update=updates)
    if not entry.source_service:  # type: ignore[attr-defined]
        raise LMSConfigError(
            "source_service is required: pass it to the client (source_service=...) or the entry"
        )
    if not entry.environment:  # type: ignore[attr-defined]
        raise LMSConfigError(
            "environment is required: pass it to the client (environment=...) or the entry"
        )
    return entry


def encode(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def dump(entry: BaseModel) -> dict[str, Any]:
    return entry.model_dump(mode="json", exclude_none=True)


def build_app_entry(
    config: ClientConfig,
    level: LogLevel | str,
    message: str,
    context: Mapping[str, Any] | None,
    trace_id: str | None,
    created_at: datetime | None,
    source_service: str | None,
    environment: str | None,
) -> AppLogRequest:
    data: dict[str, Any] = {
        "level": level,
        "message": message,
        "context_json": dict(context) if context else None,
        "trace_id": trace_id,
        "source_service": source_service,
        "environment": environment,
    }
    if created_at is not None:
        data["created_at"] = created_at
    return apply_defaults(AppLogRequest(**data), config)


def build_audit_entry(config: ClientConfig, **fields: Any) -> AuditLogRequest:
    return apply_defaults(
        AuditLogRequest(**{k: v for k, v in fields.items() if v is not None}), config
    )


def default_headers(config: ClientConfig) -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": "application/json", **config.headers}


def error_from_response(response: httpx.Response) -> LMSError:
    try:
        body = response.json()
        if not isinstance(body, dict):
            body = {"raw": body}
    except ValueError:
        body = {"raw": response.text}
    message = str(body.get("message") or body.get("detail") or response.reason_phrase)
    status = response.status_code
    if status == 429:
        return LMSRateLimitError(
            message, status, body, retry_after=parse_retry_after(response.headers)
        )
    cls = _STATUS_ERRORS.get(status, LMSServerError if status >= 500 else LMSError)
    return cls(message, status, body)


def parse_retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


def should_retry(response: httpx.Response) -> bool:
    return response.status_code in _RETRY_STATUSES


def retry_delay(config: ClientConfig, attempt: int, response: httpx.Response | None) -> float:
    """Exponential backoff with jitter; honours Retry-After (capped by max_backoff)."""
    if response is not None and response.status_code == 429:
        retry_after = parse_retry_after(response.headers)
        if retry_after is not None:
            return min(retry_after, config.max_backoff)
    base = min(config.max_backoff, config.backoff * (2 ** (attempt - 1)))
    return float(base * (0.5 + random.random() / 2))
