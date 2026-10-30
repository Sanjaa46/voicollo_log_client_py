import hashlib
import hmac
import json

import httpx
import pytest

BASE = "https://lms.test"
SECRET = "test-ingest-secret"


def verify(request: httpx.Request) -> dict:
    """Re-implementation of the LMS server check (app/core/dependencies.py)."""
    ts = request.headers["X-Timestamp"]
    expected = hmac.new(
        SECRET.encode(), ts.encode() + b"." + request.content, hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(expected, request.headers["X-Signature"]), "bad signature"
    return json.loads(request.content)


@pytest.fixture
def ok_ingest():
    def handler(request: httpx.Request) -> httpx.Response:
        verify(request)
        return httpx.Response(201, json={"log_id": 7, "ingested_at": "2026-01-01T00:00:00.000Z"})

    return handler
