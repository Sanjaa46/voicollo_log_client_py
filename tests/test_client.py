import logging
from datetime import datetime, timedelta, timezone

import httpx
import pydantic
import pytest
import respx

from lms_logger import (
    AppLogRequest,
    AsyncLMSClient,
    AuditLogRequest,
    LMSAuthError,
    LMSClient,
    LMSConfigError,
    LMSConnectionError,
    LMSPermissionError,
    LMSRateLimitError,
    LMSServerError,
    LMSValidationError,
    LogLevel,
    __version__,
)
from lms_logger.signing import generate_signature
from tests.conftest import BASE, SECRET, verify


def client(**kw) -> LMSClient:
    opts = dict(secret=SECRET, source_service="billing", environment="production", backoff=0.01)
    opts.update(kw)
    return LMSClient(BASE, **opts)


# ------------------------------------------------------------------- signing
def test_signature_matches_server_algorithm():
    sig = generate_signature("s", b'{"a":1}', "1700000000")
    # Known-answer vector shared with the JS package and the server tests.
    assert sig == "ab7bd212df57f3ef4ea3c17d4a1a71d8981ef595d4cd699b62517c68d89192a2"


@respx.mock
def test_info_applies_defaults_and_signs(ok_ingest):
    route = respx.post(f"{BASE}/api/v1/logs/app").mock(side_effect=ok_ingest)
    with client() as lms:
        result = lms.info("Payment captured", {"order": 42}, trace_id="t-1")
    assert result.log_id == 7
    body = verify(route.calls.last.request)
    assert body["source_service"] == "billing"
    assert body["environment"] == "production"
    assert body["level"] == "INFO"
    assert body["context_json"] == {"order": 42}
    assert body["trace_id"] == "t-1"
    assert body["created_at"].endswith("Z")
    assert route.calls.last.request.headers["User-Agent"] == f"lms-logger-python/{__version__}"


@respx.mock
def test_naive_datetime_is_treated_as_local_time(ok_ingest):
    route = respx.post(f"{BASE}/api/v1/logs/app").mock(side_effect=ok_ingest)
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with client() as lms:
        lms.log("warning", "x", created_at=naive)
    body = verify(route.calls.last.request)
    expected = naive.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    assert body["created_at"] == expected.replace("+00:00", "Z")
    assert body["level"] == "WARN"


def test_missing_source_service_is_a_config_error():
    with LMSClient(BASE, secret=SECRET, environment="p") as lms, pytest.raises(LMSConfigError):
        lms.info("x")


def test_secret_required():
    with pytest.raises(LMSConfigError):
        LMSClient(BASE)


def test_local_validation_matches_server():
    with pytest.raises(pydantic.ValidationError):
        AppLogRequest(level="INFO", message="   ")
    with pytest.raises(pydantic.ValidationError):
        AuditLogRequest(
            actor_id="u",
            actor_type="USER",
            action="user login",
            target_resource="/",
            outcome="SUCCESS",
        )
    with pytest.raises(pydantic.ValidationError):
        AuditLogRequest(
            actor_id="u",
            actor_type="USER",
            action="X",
            target_resource="/",
            outcome="SUCCESS",
            ip_address="not-an-ip",
        )
    assert LogLevel.WARNING is LogLevel.WARN


@respx.mock
def test_audit(ok_ingest):
    route = respx.post(f"{BASE}/api/v1/logs/audit").mock(side_effect=ok_ingest)
    with client() as lms:
        lms.audit(
            actor_id="u-1",
            actor_type="user",
            action="record_delete",
            target_resource="/r/1",
            outcome="success",
            ip_address="::1",
            detail={"k": 1},
        )
    body = verify(route.calls.last.request)
    assert body["action"] == "RECORD_DELETE"
    assert body["actor_type"] == "USER" and body["outcome"] == "SUCCESS"
    assert body["detail_json"] == {"k": 1}
    assert body["environment"] == "production"


# ------------------------------------------------------------------- errors & retries
@pytest.mark.parametrize(
    ("status", "error_code", "exc"),
    [
        (401, "SIGNATURE_INVALID", LMSAuthError),
        (403, "SERVICE_DISABLED", LMSPermissionError),
        (422, "SERVICE_NOT_REGISTERED", LMSValidationError),
    ],
)
@respx.mock
def test_client_errors_are_not_retried(status, error_code, exc):
    route = respx.post(f"{BASE}/api/v1/logs/app").respond(
        status, json={"error_code": error_code, "message": "nope", "request_id": "r1"}
    )
    with client() as lms, pytest.raises(exc) as info:
        lms.info("x")
    assert route.call_count == 1
    assert info.value.error_code == error_code
    assert info.value.request_id == "r1"


@respx.mock
def test_server_errors_are_retried_with_fresh_signature(ok_ingest):
    responses = iter([httpx.Response(503), httpx.Response(502)])

    def handler(request):
        try:
            return next(responses)
        except StopIteration:
            return ok_ingest(request)

    route = respx.post(f"{BASE}/api/v1/logs/app").mock(side_effect=handler)
    with client() as lms:
        assert lms.error("boom").log_id == 7
    assert route.call_count == 3
    stamps = [c.request.headers["X-Timestamp"] for c in route.calls]
    assert len(set(stamps)) == 3  # every attempt re-signed, never a replay


@respx.mock
def test_retries_exhausted_raise_last_error():
    route = respx.post(f"{BASE}/api/v1/logs/app").respond(500, json={"message": "down"})
    with client(max_retries=2) as lms, pytest.raises(LMSServerError):
        lms.info("x")
    assert route.call_count == 3


@respx.mock
def test_rate_limit_honours_retry_after(monkeypatch, ok_ingest):
    sleeps = []
    monkeypatch.setattr("lms_logger.client.time.sleep", sleeps.append)
    calls = iter([httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow"})])
    route = respx.post(f"{BASE}/api/v1/logs/app").mock(
        side_effect=lambda r: next(calls, None) or ok_ingest(r)
    )
    with client() as lms:
        lms.info("x")
    assert sleeps == [7.0] and route.call_count == 2


@respx.mock
def test_rate_limit_error_exposes_retry_after():
    respx.post(f"{BASE}/api/v1/logs/app").respond(429, headers={"Retry-After": "3"}, json={})
    with client(max_retries=0) as lms, pytest.raises(LMSRateLimitError) as info:
        lms.info("x")
    assert info.value.retry_after == 3.0


@respx.mock
def test_connection_errors_become_lms_connection_error():
    respx.post(f"{BASE}/api/v1/logs/app").mock(side_effect=httpx.ConnectError("refused"))
    with client(max_retries=1) as lms, pytest.raises(LMSConnectionError):
        lms.info("x")


# ------------------------------------------------------------------- batch & buffered
@respx.mock
def test_batch_reports_failures():
    respx.post(f"{BASE}/api/v1/logs/app/batch").respond(
        207,
        json={
            "results": [
                {"index": 0, "success": True, "log_id": 1},
                {"index": 1, "success": False, "error_code": "VALIDATION_ERROR", "message": "bad"},
            ],
            "total": 2,
            "succeeded": 1,
            "failed": 1,
        },
    )
    with client() as lms:
        result = lms.log_app_batch(
            [AppLogRequest(level="INFO", message="a"), AppLogRequest(level="INFO", message="b")]
        )
    assert [f.index for f in result.failures] == [1]


@respx.mock
def test_buffered_mode_batches_and_never_raises():
    batches = []

    def handler(request):
        body = verify(request)
        batches.append(body["logs"])
        n = len(body["logs"])
        return httpx.Response(
            207,
            json={
                "results": [{"index": i, "success": True, "log_id": i} for i in range(n)],
                "total": n,
                "succeeded": n,
                "failed": 0,
            },
        )

    respx.post(f"{BASE}/api/v1/logs/app/batch").mock(side_effect=handler)
    lms = client(buffered=True, batch_size=10, flush_interval=0.05)
    for i in range(25):
        assert lms.info(f"m{i}") is None
    assert lms.flush(5)
    lms.close()
    assert sum(len(b) for b in batches) == 25
    assert max(len(b) for b in batches) <= 10


@respx.mock
def test_buffered_failures_go_to_on_error():
    respx.post(f"{BASE}/api/v1/logs/app").respond(403, json={"error_code": "SERVICE_DISABLED"})
    errors = []
    lms = client(buffered=True, flush_interval=0.01, on_error=lambda e, p: errors.append((e, p)))
    lms.info("dropped")
    lms.close()
    assert isinstance(errors[0][0], LMSPermissionError)
    assert errors[0][1].message == "dropped"


# ------------------------------------------------------------------- logging handler
@respx.mock
def test_logging_handler(ok_ingest):
    route = respx.post(f"{BASE}/api/v1/logs/app").mock(side_effect=ok_ingest)
    lms = client(buffered=True, flush_interval=0.01)
    log = logging.getLogger("myapp.test")
    log.setLevel(logging.DEBUG)
    handler = lms.handler(level=logging.INFO)
    log.addHandler(handler)
    logging.getLogger("httpx").addHandler(handler)  # must be ignored (no feedback loop)
    try:
        log.debug("ignored")
        try:
            raise ZeroDivisionError("division by zero")
        except ZeroDivisionError:
            log.exception("failed %s", "op", extra={"order_id": 5, "trace_id": "abc"})
        assert lms.flush(5)
    finally:
        log.removeHandler(handler)
        logging.getLogger("httpx").removeHandler(handler)
        lms.close()
    assert route.call_count == 1
    body = verify(route.calls.last.request)
    assert body["level"] == "ERROR" and body["message"] == "failed op"
    assert body["trace_id"] == "abc"
    assert body["context_json"]["order_id"] == 5
    assert "ZeroDivisionError" in body["context_json"]["exception"]


# ------------------------------------------------------------------- async
@respx.mock
async def test_async_client(ok_ingest):
    route = respx.post(f"{BASE}/api/v1/logs/app").mock(side_effect=ok_ingest)
    async with AsyncLMSClient(
        BASE, secret=SECRET, source_service="billing", environment="production"
    ) as lms:
        result = await lms.warn("slow", {"ms": 900})
    assert result.log_id == 7
    assert verify(route.calls.last.request)["level"] == "WARN"


@respx.mock
async def test_async_buffered_flush(ok_ingest):
    respx.post(f"{BASE}/api/v1/logs/app").mock(side_effect=ok_ingest)
    respx.post(f"{BASE}/api/v1/logs/audit").mock(side_effect=ok_ingest)
    lms = AsyncLMSClient(
        BASE, secret=SECRET, source_service="b", environment="p", buffered=True, flush_interval=0.01
    )
    assert await lms.info("one") is None
    assert (
        await lms.audit(
            actor_id="u", actor_type="USER", action="A", target_resource="/", outcome="SUCCESS"
        )
        is None
    )
    assert await lms.flush(5)
    await lms.close()
    assert respx.calls.call_count == 2


def test_deprecated_service_id_maps_to_source_service():
    with pytest.warns(DeprecationWarning):
        lms = LMSClient(BASE, secret=SECRET, service_id="legacy")
    assert lms.source_service == "legacy"
    lms.close()


def test_created_at_future_guard_is_server_side():
    # The client does not second-guess clocks; it only formats UTC.
    entry = AppLogRequest(
        level="INFO", message="x", created_at=datetime.now(timezone.utc) + timedelta(days=9)
    )
    assert entry.model_dump(mode="json")["created_at"].endswith("Z")
