"""Sync and async clients for the Voicollo LMS ingestion API."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import queue
import threading
import time
import warnings
import weakref
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

import httpx

from . import _core
from ._core import AUDIT_PATH, BATCH_PATH, MAX_BATCH, ClientConfig, ErrorHandler, RequestSigner
from .exceptions import LMSConnectionError, LMSError, LMSQueueFullError
from .models import (
    ActorType,
    AppLogRequest,
    AuditLogRequest,
    BatchAppLogResponse,
    IngestResponse,
    LogLevel,
    Outcome,
)

logger = logging.getLogger("lms_logger")

_Kind = Literal["app", "audit"]


def _config(
    base_url: str,
    secret: str | None,
    service_id: str | None,
    **options: Any,
) -> ClientConfig:
    if service_id is not None:
        warnings.warn(
            "service_id is deprecated and was never sent to the server; "
            "use source_service=... instead",
            DeprecationWarning,
            stacklevel=3,
        )
        if options.get("source_service") is None:
            options["source_service"] = service_id
    return ClientConfig(base_url=base_url, secret=secret or "", **options)


def _report(config: ClientConfig, exc: Exception, payload: Any) -> None:
    if config.on_error is not None:
        try:
            config.on_error(exc, payload)
            return
        except Exception:  # never let a user callback break the worker
            logger.exception("lms_logger on_error callback raised")
    logger.warning("lms_logger dropped a log entry: %s", exc)


# =============================================================================
# Sync client
# =============================================================================
class LMSClient:
    """Thread-safe synchronous client.

    Direct mode (default): every call sends immediately and raises :class:`LMSError`
    on failure. Buffered mode (``buffered=True``): calls enqueue and return at once;
    a background thread ships batches and reports failures to ``on_error`` — logging
    can never raise into your application.

    Example::

        lms = LMSClient(
            base_url="https://lms.example.com",
            secret=os.environ["LMS_INGEST_SECRET"],
            source_service="billing-api",
            environment="production",
        )
        lms.info("Payment captured", context={"order_id": 42})
        lms.close()
    """

    def __init__(
        self,
        base_url: str,
        *,
        secret: str | None = None,
        source_service: str | None = None,
        environment: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        max_backoff: float = 30.0,
        buffered: bool = False,
        batch_size: int = 50,
        flush_interval: float = 2.0,
        max_queue: int = 10_000,
        on_error: ErrorHandler | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        service_id: str | None = None,
    ) -> None:
        self._config = _config(
            base_url,
            secret,
            service_id,
            source_service=source_service,
            environment=environment,
            timeout=timeout,
            max_retries=max_retries,
            backoff=backoff,
            max_backoff=max_backoff,
            buffered=buffered,
            batch_size=batch_size,
            flush_interval=flush_interval,
            max_queue=max_queue,
            on_error=on_error,
            headers=dict(headers or {}),
        )
        self._signer = RequestSigner(self._config.secret)
        self._sign_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=self._config.base_url,
            timeout=timeout,
            headers=_core.default_headers(self._config),
            transport=transport,
        )
        self._closed = False
        self._buffer: _ThreadBuffer | None = _ThreadBuffer(self) if buffered else None

    # ------------------------------------------------------------ properties
    @property
    def source_service(self) -> str | None:
        return self._config.source_service

    @property
    def service_id(self) -> str | None:  # backwards compatibility
        return self._config.source_service

    @property
    def buffered(self) -> bool:
        return self._buffer is not None

    # ------------------------------------------------------------ transport
    def _post(self, path: str, payload: Any) -> dict[str, Any]:
        body = _core.encode(payload)
        last_error: Exception | None = None
        for attempt in range(1, self._config.max_retries + 2):
            with self._sign_lock:
                headers = self._signer.headers(body)
            response: httpx.Response | None = None
            try:
                response = self._http.post(path, content=body, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.is_success:
                    data: dict[str, Any] = response.json()
                    return data
                error = _core.error_from_response(response)
                if not _core.should_retry(response):
                    raise error
                last_error = error
            if attempt > self._config.max_retries:
                break
            delay = _core.retry_delay(self._config, attempt, response)
            logger.debug("LMS request failed (%s); retry %d in %.2fs", last_error, attempt, delay)
            time.sleep(delay)
        if isinstance(last_error, LMSError):
            raise last_error
        raise LMSConnectionError(
            f"LMS unreachable after {self._config.max_retries + 1} attempts: {last_error}"
        ) from last_error

    def _send_app(self, entry: AppLogRequest) -> IngestResponse:
        return IngestResponse.model_validate(self._post(_core.APP_PATH, _core.dump(entry)))

    def _send_audit(self, entry: AuditLogRequest) -> IngestResponse:
        return IngestResponse.model_validate(self._post(AUDIT_PATH, _core.dump(entry)))

    def _send_batch(self, entries: Sequence[AppLogRequest]) -> BatchAppLogResponse:
        data = self._post(BATCH_PATH, {"logs": [_core.dump(e) for e in entries]})
        return BatchAppLogResponse.model_validate(data)

    # ------------------------------------------------------------ explicit API
    def log_app(self, request: AppLogRequest) -> IngestResponse | None:
        """Send one application log. Returns ``None`` in buffered mode."""
        entry = _core.apply_defaults(request, self._config)
        if self._buffer:
            self._buffer.put("app", entry)
            return None
        return self._send_app(entry)

    def log_app_batch(self, requests: Sequence[AppLogRequest]) -> BatchAppLogResponse:
        """Send up to 100 entries in one request (always immediate). Partial success
        is reported per entry in the response — inspect ``.failures``."""
        if not requests:
            raise ValueError("requests must not be empty")
        if len(requests) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} entries per batch")
        return self._send_batch([_core.apply_defaults(r, self._config) for r in requests])

    def log_audit(self, request: AuditLogRequest) -> IngestResponse | None:
        """Send one audit log (immutable in the LMS). Returns ``None`` in buffered mode."""
        entry = _core.apply_defaults(request, self._config)
        if self._buffer:
            self._buffer.put("audit", entry)
            return None
        return self._send_audit(entry)

    # ------------------------------------------------------------ convenience
    def log(
        self,
        level: LogLevel | str,
        message: str,
        context: Mapping[str, Any] | None = None,
        *,
        trace_id: str | None = None,
        created_at: datetime | None = None,
        source_service: str | None = None,
        environment: str | None = None,
    ) -> IngestResponse | None:
        entry = _core.build_app_entry(
            self._config, level, message, context, trace_id, created_at, source_service, environment
        )
        return self.log_app(entry)

    def debug(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return self.log(LogLevel.DEBUG, message, context, **kw)

    def info(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return self.log(LogLevel.INFO, message, context, **kw)

    def warn(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return self.log(LogLevel.WARN, message, context, **kw)

    warning = warn

    def error(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return self.log(LogLevel.ERROR, message, context, **kw)

    def fatal(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return self.log(LogLevel.FATAL, message, context, **kw)

    critical = fatal

    def audit(
        self,
        *,
        actor_id: str,
        actor_type: ActorType | str,
        action: str,
        target_resource: str,
        outcome: Outcome | str,
        ip_address: str | None = None,
        detail: Mapping[str, Any] | None = None,
        event_time: datetime | None = None,
        source_service: str | None = None,
        environment: str | None = None,
    ) -> IngestResponse | None:
        entry = _core.build_audit_entry(
            self._config,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            target_resource=target_resource,
            outcome=outcome,
            ip_address=ip_address,
            detail_json=dict(detail) if detail else None,
            event_time=event_time,
            source_service=source_service,
            environment=environment,
        )
        return self.log_audit(entry)

    def handler(self, level: int = logging.INFO, **kwargs: Any) -> logging.Handler:
        """A stdlib :class:`logging.Handler` that forwards records to this client."""
        from .handler import LMSHandler

        return LMSHandler(self, level=level, **kwargs)

    # ------------------------------------------------------------ lifecycle
    def flush(self, timeout: float | None = 10.0) -> bool:
        """Buffered mode: wait until queued entries are sent. True if fully drained."""
        return self._buffer.flush(timeout) if self._buffer else True

    def close(self, timeout: float | None = 10.0) -> None:
        """Flush (buffered mode) and release connections. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        if self._buffer:
            self._buffer.stop(timeout)
        self._http.close()

    def __enter__(self) -> LMSClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _ThreadBuffer:
    """Background batching for :class:`LMSClient`."""

    def __init__(self, client: LMSClient) -> None:
        self._client = weakref.proxy(client)
        self._config = client._config
        self._queue: queue.Queue[tuple[_Kind, Any] | None] = queue.Queue(self._config.max_queue)
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="lms-logger", daemon=True)
        self._thread.start()
        atexit.register(self.stop, 5.0)

    def put(self, kind: _Kind, entry: Any) -> None:
        try:
            self._queue.put_nowait((kind, entry))
        except queue.Full:
            self.dropped += 1
            _report(self._config, LMSQueueFullError("lms_logger queue is full"), entry)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            items = [item]
            deadline = time.monotonic() + self._config.flush_interval
            stop = False
            while len(items) < self._config.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    nxt = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    self._queue.task_done()
                    break
                items.append(nxt)
            self._ship(items)
            for _ in items:
                self._queue.task_done()
            if stop:
                return

    def _ship(self, items: list[tuple[_Kind, Any]]) -> None:
        apps = [entry for kind, entry in items if kind == "app"]
        audits = [entry for kind, entry in items if kind == "audit"]
        try:
            if len(apps) == 1:
                self._client._send_app(apps[0])
            elif apps:
                result = self._client._send_batch(apps)
                for failure in result.failures:
                    _report(
                        self._config,
                        LMSError(
                            failure.message or "entry rejected", error_code=failure.error_code
                        ),
                        apps[failure.index],
                    )
        except Exception as exc:
            for entry in apps:
                _report(self._config, exc, entry)
        for entry in audits:
            try:
                self._client._send_audit(entry)
            except Exception as exc:
                _report(self._config, exc, entry)

    def flush(self, timeout: float | None) -> bool:
        # Queue.all_tasks_done shares the queue mutex, so this cannot race with put().
        with self._queue.all_tasks_done:
            return self._queue.all_tasks_done.wait_for(
                lambda: self._queue.unfinished_tasks == 0, timeout
            )

    def stop(self, timeout: float | None) -> None:
        atexit.unregister(self.stop)
        if not self._thread.is_alive():
            return
        self.flush(timeout)
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout)


# =============================================================================
# Async client
# =============================================================================
class AsyncLMSClient:
    """asyncio client with the same API as :class:`LMSClient` (methods are awaitable).

    Use it as ``async with AsyncLMSClient(...) as lms:`` or call ``await lms.close()``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        secret: str | None = None,
        source_service: str | None = None,
        environment: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        max_backoff: float = 30.0,
        buffered: bool = False,
        batch_size: int = 50,
        flush_interval: float = 2.0,
        max_queue: int = 10_000,
        on_error: ErrorHandler | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        service_id: str | None = None,
    ) -> None:
        self._config = _config(
            base_url,
            secret,
            service_id,
            source_service=source_service,
            environment=environment,
            timeout=timeout,
            max_retries=max_retries,
            backoff=backoff,
            max_backoff=max_backoff,
            buffered=buffered,
            batch_size=batch_size,
            flush_interval=flush_interval,
            max_queue=max_queue,
            on_error=on_error,
            headers=dict(headers or {}),
        )
        self._signer = RequestSigner(self._config.secret)
        self._http = httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=timeout,
            headers=_core.default_headers(self._config),
            transport=transport,
        )
        self._closed = False
        self._buffer: _TaskBuffer | None = _TaskBuffer(self) if buffered else None

    @property
    def source_service(self) -> str | None:
        return self._config.source_service

    @property
    def service_id(self) -> str | None:
        return self._config.source_service

    @property
    def buffered(self) -> bool:
        return self._buffer is not None

    async def _post(self, path: str, payload: Any) -> dict[str, Any]:
        body = _core.encode(payload)
        last_error: Exception | None = None
        for attempt in range(1, self._config.max_retries + 2):
            headers = self._signer.headers(body)
            response: httpx.Response | None = None
            try:
                response = await self._http.post(path, content=body, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.is_success:
                    data: dict[str, Any] = response.json()
                    return data
                error = _core.error_from_response(response)
                if not _core.should_retry(response):
                    raise error
                last_error = error
            if attempt > self._config.max_retries:
                break
            await asyncio.sleep(_core.retry_delay(self._config, attempt, response))
        if isinstance(last_error, LMSError):
            raise last_error
        raise LMSConnectionError(
            f"LMS unreachable after {self._config.max_retries + 1} attempts: {last_error}"
        ) from last_error

    async def _send_app(self, entry: AppLogRequest) -> IngestResponse:
        return IngestResponse.model_validate(await self._post(_core.APP_PATH, _core.dump(entry)))

    async def _send_audit(self, entry: AuditLogRequest) -> IngestResponse:
        return IngestResponse.model_validate(await self._post(AUDIT_PATH, _core.dump(entry)))

    async def _send_batch(self, entries: Sequence[AppLogRequest]) -> BatchAppLogResponse:
        data = await self._post(BATCH_PATH, {"logs": [_core.dump(e) for e in entries]})
        return BatchAppLogResponse.model_validate(data)

    async def log_app(self, request: AppLogRequest) -> IngestResponse | None:
        entry = _core.apply_defaults(request, self._config)
        if self._buffer:
            self._buffer.put("app", entry)
            return None
        return await self._send_app(entry)

    async def log_app_batch(self, requests: Sequence[AppLogRequest]) -> BatchAppLogResponse:
        if not requests:
            raise ValueError("requests must not be empty")
        if len(requests) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} entries per batch")
        return await self._send_batch([_core.apply_defaults(r, self._config) for r in requests])

    async def log_audit(self, request: AuditLogRequest) -> IngestResponse | None:
        entry = _core.apply_defaults(request, self._config)
        if self._buffer:
            self._buffer.put("audit", entry)
            return None
        return await self._send_audit(entry)

    async def log(
        self,
        level: LogLevel | str,
        message: str,
        context: Mapping[str, Any] | None = None,
        *,
        trace_id: str | None = None,
        created_at: datetime | None = None,
        source_service: str | None = None,
        environment: str | None = None,
    ) -> IngestResponse | None:
        entry = _core.build_app_entry(
            self._config, level, message, context, trace_id, created_at, source_service, environment
        )
        return await self.log_app(entry)

    async def debug(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return await self.log(LogLevel.DEBUG, message, context, **kw)

    async def info(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return await self.log(LogLevel.INFO, message, context, **kw)

    async def warn(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return await self.log(LogLevel.WARN, message, context, **kw)

    warning = warn

    async def error(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return await self.log(LogLevel.ERROR, message, context, **kw)

    async def fatal(
        self, message: str, context: Mapping[str, Any] | None = None, **kw: Any
    ) -> IngestResponse | None:  # noqa: E501
        return await self.log(LogLevel.FATAL, message, context, **kw)

    critical = fatal

    async def audit(
        self,
        *,
        actor_id: str,
        actor_type: ActorType | str,
        action: str,
        target_resource: str,
        outcome: Outcome | str,
        ip_address: str | None = None,
        detail: Mapping[str, Any] | None = None,
        event_time: datetime | None = None,
        source_service: str | None = None,
        environment: str | None = None,
    ) -> IngestResponse | None:
        entry = _core.build_audit_entry(
            self._config,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            target_resource=target_resource,
            outcome=outcome,
            ip_address=ip_address,
            detail_json=dict(detail) if detail else None,
            event_time=event_time,
            source_service=source_service,
            environment=environment,
        )
        return await self.log_audit(entry)

    async def flush(self, timeout: float | None = 10.0) -> bool:
        return await self._buffer.flush(timeout) if self._buffer else True

    async def close(self, timeout: float | None = 10.0) -> None:
        if self._closed:
            return
        self._closed = True
        if self._buffer:
            await self._buffer.stop(timeout)
        await self._http.aclose()

    async def __aenter__(self) -> AsyncLMSClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


class _TaskBuffer:
    """Background batching for :class:`AsyncLMSClient` (started lazily in the running loop)."""

    def __init__(self, client: AsyncLMSClient) -> None:
        self._client = client
        self._config = client._config
        self._queue: asyncio.Queue[tuple[_Kind, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0

    def _ensure_started(self) -> asyncio.Queue[tuple[_Kind, Any]]:
        if self._queue is None:
            self._queue = asyncio.Queue(self._config.max_queue)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="lms-logger")
        return self._queue

    def put(self, kind: _Kind, entry: Any) -> None:
        q = self._ensure_started()
        try:
            q.put_nowait((kind, entry))
        except asyncio.QueueFull:
            self.dropped += 1
            _report(self._config, LMSQueueFullError("lms_logger queue is full"), entry)

    async def _run(self) -> None:
        assert self._queue is not None
        q = self._queue
        while True:
            items = [await q.get()]
            deadline = time.monotonic() + self._config.flush_interval
            while len(items) < self._config.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    items.append(await asyncio.wait_for(q.get(), remaining))
                except asyncio.TimeoutError:
                    break
            try:
                await self._ship(items)
            finally:
                for _ in items:
                    q.task_done()

    async def _ship(self, items: list[tuple[_Kind, Any]]) -> None:
        apps = [entry for kind, entry in items if kind == "app"]
        audits = [entry for kind, entry in items if kind == "audit"]
        try:
            if len(apps) == 1:
                await self._client._send_app(apps[0])
            elif apps:
                result = await self._client._send_batch(apps)
                for failure in result.failures:
                    _report(
                        self._config,
                        LMSError(
                            failure.message or "entry rejected", error_code=failure.error_code
                        ),
                        apps[failure.index],
                    )
        except Exception as exc:
            for entry in apps:
                _report(self._config, exc, entry)
        for entry in audits:
            try:
                await self._client._send_audit(entry)
            except Exception as exc:
                _report(self._config, exc, entry)

    async def flush(self, timeout: float | None) -> bool:
        if self._queue is None:
            return True
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self, timeout: float | None) -> None:
        await self.flush(timeout)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
