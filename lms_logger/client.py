from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .exceptions import (
    LMSAuthError,
    LMSConnectionError,
    LMSError,
    LMSRateLimitError,
    LMSValidationError,
)
from .models import (
    AppLogRequest,
    AppLogResponse,
    AuditLogRequest,
    AuditLogResponse,
    BatchAppLogRequest,
    BatchAppLogResponse,
)
from .signing import current_timestamp, generate_signature

logger = logging.getLogger(__name__)

_STATUS_MAP: dict[int, type[LMSError]] = {
    401: LMSAuthError,
    429: LMSRateLimitError,
    422: LMSValidationError,
}


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    exc_class = _STATUS_MAP.get(response.status_code, LMSError)
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    raise exc_class(
        message=body.get("detail") or body.get("message") or response.text,
        status_code=response.status_code,
        response_body=body,
    )


class LMSClient:
    """
    Synchronous LMS client.

    Usage::

        from lms_logger import LMSClient, AppLogRequest, LogLevel

        client = LMSClient(
            base_url="https://lms.example.gov.mn",
            service_id="your-service-uuid-no-dashes",
            secret="your-hmac-secret",
        )

        client.log_app(AppLogRequest(
            service_id=client.service_id,
            level=LogLevel.INFO,
            message="Service started",
        ))

        client.close()

    Or use as a context manager::

        with LMSClient(...) as client:
            client.log_app(...)
    """

    def __init__(
        self,
        base_url: str,
        service_id: str,
        secret: str,
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        self.service_id = service_id
        self._secret = secret
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries
        self._http = httpx.Client(timeout=timeout)

    # ── internal ─────────────────────────────────────────────────────────────

    def _signed_headers(self, body: bytes) -> dict[str, str]:
        ts = current_timestamp()
        sig = generate_signature(self._secret, body, ts)
        return {
            "Content-Type": "application/json",
            "x-timestamp": ts,
            "x-signature": sig,
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, default=str).encode()
        headers = self._signed_headers(body)
        url = f"{self._base}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._http.post(url, content=body, headers=headers)
                _raise_for_status(response)
                return response.json()
            except LMSRateLimitError:
                raise  # never retry 429 — caller must back off
            except LMSError:
                raise
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("LMS request attempt %d/%d failed: %s", attempt, self._max_retries, exc)

        raise LMSConnectionError(
            f"LMS unreachable after {self._max_retries} attempts: {last_exc}"
        )

    # ── public API ────────────────────────────────────────────────────────────

    def log_app(self, request: AppLogRequest) -> AppLogResponse:
        """Send a single application log entry."""
        data = self._post("/api/v1/logs/app", request.model_dump())
        return AppLogResponse.model_validate(data)

    def log_app_batch(self, requests: list[AppLogRequest]) -> BatchAppLogResponse:
        """Send multiple application log entries in one HTTP call."""
        payload = BatchAppLogRequest(logs=requests)
        data = self._post("/api/v1/logs/app/batch", payload.model_dump())
        return BatchAppLogResponse.model_validate(data)

    def log_audit(self, request: AuditLogRequest) -> AuditLogResponse:
        """Send an audit log entry (immutable — DELETE is blocked by LMS)."""
        data = self._post("/api/v1/logs/audit", request.model_dump())
        return AuditLogResponse.model_validate(data)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LMSClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class AsyncLMSClient:
    """
    Async LMS client — drop-in for FastAPI / async services.

    Usage::

        async with AsyncLMSClient(...) as client:
            await client.log_app(AppLogRequest(...))
    """

    def __init__(
        self,
        base_url: str,
        service_id: str,
        secret: str,
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        self.service_id = service_id
        self._secret = secret
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries
        self._http = httpx.AsyncClient(timeout=timeout)

    def _signed_headers(self, body: bytes) -> dict[str, str]:
        ts = current_timestamp()
        sig = generate_signature(self._secret, body, ts)
        return {
            "Content-Type": "application/json",
            "x-timestamp": ts,
            "x-signature": sig,
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, default=str).encode()
        headers = self._signed_headers(body)
        url = f"{self._base}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._http.post(url, content=body, headers=headers)
                _raise_for_status(response)
                return response.json()
            except LMSRateLimitError:
                raise
            except LMSError:
                raise
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("LMS async request attempt %d/%d failed: %s", attempt, self._max_retries, exc)

        raise LMSConnectionError(
            f"LMS unreachable after {self._max_retries} attempts: {last_exc}"
        )

    async def log_app(self, request: AppLogRequest) -> AppLogResponse:
        data = await self._post("/api/v1/logs/app", request.model_dump())
        return AppLogResponse.model_validate(data)

    async def log_app_batch(self, requests: list[AppLogRequest]) -> BatchAppLogResponse:
        payload = BatchAppLogRequest(logs=requests)
        data = await self._post("/api/v1/logs/app/batch", payload.model_dump())
        return BatchAppLogResponse.model_validate(data)

    async def log_audit(self, request: AuditLogRequest) -> AuditLogResponse:
        data = await self._post("/api/v1/logs/audit", request.model_dump())
        return AuditLogResponse.model_validate(data)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncLMSClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()