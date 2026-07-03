"""
Tests — run with:  pytest tests/ -v
Requires dev extras:  pip install -e ".[dev]"
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
import respx
import httpx

from lms_logger import LMSClient, AppLogRequest, AuditLogRequest, LogLevel
from lms_logger.client import AsyncLMSClient
from lms_logger.exceptions import LMSAuthError, LMSRateLimitError
from lms_logger.signing import generate_signature, current_timestamp

BASE = "https://lms.test"
SECRET = "test-secret"
SVC = "abc123"


# ── signing ───────────────────────────────────────────────────────────────────

def test_signature_is_deterministic():
    body = b'{"foo":"bar"}'
    ts = "1700000000"
    sig1 = generate_signature(SECRET, body, ts)
    sig2 = generate_signature(SECRET, body, ts)
    assert sig1 == sig2


def test_signature_changes_with_body():
    ts = "1700000000"
    s1 = generate_signature(SECRET, b"aaa", ts)
    s2 = generate_signature(SECRET, b"bbb", ts)
    assert s1 != s2


def test_signature_matches_manual_hmac():
    body = b'{"x":1}'
    ts = "1700000000"
    message = f"{ts}.".encode() + body
    expected = hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    assert generate_signature(SECRET, body, ts) == expected


# ── sync client ───────────────────────────────────────────────────────────────

@respx.mock
def test_log_app_success():
    fake_resp = {
        "id": 1, "service_id": SVC, "level": "INFO",
        "message": "hello", "timestamp": "2024-01-01T00:00:00", "extra": None,
    }
    respx.post(f"{BASE}/api/v1/logs/app").mock(return_value=httpx.Response(201, json=fake_resp))

    with LMSClient(base_url=BASE, service_id=SVC, secret=SECRET) as client:
        resp = client.log_app(AppLogRequest(service_id=SVC, level=LogLevel.INFO, message="hello"))

    assert resp.id == 1
    assert resp.level == "INFO"


@respx.mock
def test_log_app_auth_error_raises():
    respx.post(f"{BASE}/api/v1/logs/app").mock(
        return_value=httpx.Response(401, json={"detail": "invalid signature"})
    )
    with LMSClient(base_url=BASE, service_id=SVC, secret=SECRET) as client:
        with pytest.raises(LMSAuthError):
            client.log_app(AppLogRequest(service_id=SVC, level=LogLevel.ERROR, message="x"))


@respx.mock
def test_rate_limit_not_retried():
    route = respx.post(f"{BASE}/api/v1/logs/app").mock(
        return_value=httpx.Response(429, json={"detail": "slow down"})
    )
    with LMSClient(base_url=BASE, service_id=SVC, secret=SECRET, max_retries=3) as client:
        with pytest.raises(LMSRateLimitError):
            client.log_app(AppLogRequest(service_id=SVC, level=LogLevel.INFO, message="x"))
    # Should only be called once despite max_retries=3
    assert route.call_count == 1


@respx.mock
def test_log_audit_success():
    fake_resp = {
        "id": 99, "service_id": SVC, "actor_id": "u1",
        "action": "DELETE", "resource": "user", "resource_id": "42",
        "timestamp": "2024-01-01T12:00:00", "meta": None,
    }
    respx.post(f"{BASE}/api/v1/logs/audit").mock(return_value=httpx.Response(201, json=fake_resp))

    with LMSClient(base_url=BASE, service_id=SVC, secret=SECRET) as client:
        resp = client.log_audit(AuditLogRequest(
            service_id=SVC, actor_id="u1", action="DELETE", resource="user", resource_id="42"
        ))

    assert resp.id == 99
    assert resp.action == "DELETE"


# ── async client ──────────────────────────────────────────────────────────────

@respx.mock
async def test_async_log_app_success():
    fake_resp = {
        "id": 2, "service_id": SVC, "level": "WARNING",
        "message": "async test", "timestamp": "2024-06-01T00:00:00", "extra": None,
    }
    respx.post(f"{BASE}/api/v1/logs/app").mock(return_value=httpx.Response(201, json=fake_resp))

    async with AsyncLMSClient(base_url=BASE, service_id=SVC, secret=SECRET) as client:
        resp = await client.log_app(
            AppLogRequest(service_id=SVC, level=LogLevel.WARNING, message="async test")
        )

    assert resp.id == 2