# lms-logger (Python)

Python client for the **Voicollo Log Management System**.

- HMAC-SHA256 request signing. Each retry is signed fresh, so it is never seen as a replay.
- Automatic retries with jittered backoff on network errors, 5xx and 429 (`Retry-After`).
- Field validation that matches the server, so mistakes fail locally.
- Sync and asyncio clients, plus a stdlib `logging` handler.
- **Buffered mode**: background batching. Logging never raises into your app.

Requires Python ≥ 3.10 and an LMS server ≥ 2.0.

## Install

Pin a release tag:

```bash
pip install "lms-logger @ git+https://github.com/Sanjaa46/voicollo_log_client_py@v1.0.0"
```

In `requirements.txt`:

```
lms-logger @ git+https://github.com/Sanjaa46/voicollo_log_client_py@v1.0.0
```

## Configure

Register your service in the LMS console (**Services → Register**), then give
your app three values:

| Variable | Value |
|---|---|
| `LMS_URL` | the LMS address, e.g. `https://lms.example.com` |
| `LMS_INGEST_SECRET` | the LMS `INGEST_SIGNING_SECRET` |
| `LMS_SOURCE_SERVICE` | your registered service id |

## Quick start

```python
import os
from lms_logger import LMSClient

lms = LMSClient(
    base_url=os.environ["LMS_URL"],
    secret=os.environ["LMS_INGEST_SECRET"],
    source_service=os.environ["LMS_SOURCE_SERVICE"],
    environment="production",
)

lms.info("Payment captured", context={"order_id": 8812, "amount": 129000})
lms.error("Gateway timeout", context={"gateway": "golomt"}, trace_id=request_id)

lms.audit(
    actor_id=str(user.id),
    actor_type="USER",            # USER | SERVICE | SYSTEM
    action="RECORD_DELETE",       # VERB_NOUN
    target_resource="/records/42",
    outcome="SUCCESS",            # SUCCESS | FAILURE | PARTIAL
    ip_address=request.client.host,
    detail={"reason": "duplicate"},
)

lms.close()   # or: with LMSClient(...) as lms:
```

Levels: `debug`, `info`, `warn` (`warning`), `error`, `fatal` (`critical`), or
`lms.log("WARN", ...)`. Timestamps default to now. Naive `datetime`s are read as
local time and sent in UTC.

## Buffered mode (recommended for web apps)

```python
lms = LMSClient(..., buffered=True, batch_size=50, flush_interval=2.0,
                on_error=lambda exc, entry: sentry_sdk.capture_exception(exc))

lms.info("queued, returns None immediately")
lms.flush()   # optional: wait until sent
lms.close()   # drains the queue (also runs automatically at interpreter exit)
```

A background thread batches app logs into `/logs/app/batch`; audit logs are
sent one by one. Failures and dropped entries (when the queue is full,
`max_queue=10000`) are passed to `on_error`. Without `on_error` they are logged
as warnings on the `lms_logger` logger.

## stdlib `logging`

```python
import logging

lms = LMSClient(..., buffered=True)
logging.getLogger().addHandler(lms.handler(level=logging.INFO))

log = logging.getLogger(__name__)
log.info("user signed in", extra={"user_id": 7, "trace_id": "abc123"})
log.exception("payment failed")    # traceback goes to context_json.exception
```

`extra=` fields go into `context_json`, and `extra={"trace_id": ...}` sets the
trace id. Records from `httpx`/`httpcore`/`lms_logger` are ignored, so the
handler can never loop on its own traffic.

## asyncio

```python
from lms_logger import AsyncLMSClient

async with AsyncLMSClient(base_url=..., secret=..., source_service="billing-api",
                          environment="production") as lms:
    await lms.info("started")
    await lms.audit(actor_id="u-1", actor_type="USER", action="USER_LOGIN",
                    target_resource="/login", outcome="SUCCESS")
```

`AsyncLMSClient(buffered=True)` batches in a background task. Call
`await lms.close()` on shutdown, e.g. in a FastAPI lifespan.

## Explicit models and batches

```python
from lms_logger import AppLogRequest, AuditLogRequest, LogLevel

lms.log_app(AppLogRequest(level=LogLevel.INFO, message="hi", context_json={"a": 1}))
result = lms.log_app_batch([AppLogRequest(level="INFO", message=f"m{i}") for i in range(100)])
for failure in result.failures:            # partial success is per entry
    print(failure.index, failure.error_code, failure.message)
```

## Errors

All exceptions inherit `LMSError` (`status_code`, `error_code`, `request_id`,
`response_body`).

| Exception | When | Retried |
|---|---|---|
| `LMSConfigError` | missing `secret` / `source_service` / `environment` | — |
| `LMSAuthError` | 401: wrong secret, or clock skew (`SIGNATURE_EXPIRED`) | no |
| `LMSPermissionError` | 403 `SERVICE_DISABLED` | no |
| `LMSValidationError` | 422: invalid payload, `SERVICE_NOT_REGISTERED` | no |
| `LMSConflictError` | 409 `DUPLICATE_REQUEST` | no |
| `LMSRateLimitError` | 429 (`.retry_after`) | yes |
| `LMSServerError` | 5xx | yes |
| `LMSConnectionError` | network / timeout after all retries | yes |

Options: `timeout=10`, `max_retries=3`, `backoff=0.5`, `max_backoff=30`,
`headers={...}`.

## Signing protocol

```
X-Timestamp: <unix seconds>
X-Signature: hex(HMAC_SHA256(secret, "<timestamp>." + raw_json_body))
```

The server rejects timestamps outside its tolerance window (default 60 s) and
any signature it has already seen.

## Development

```bash
pip install -e ".[dev]"
pytest && ruff check src tests && mypy
```

Release: bump `src/lms_logger/_version.py`, add a CHANGELOG section, open a
PR and merge it. On `main`, the Release workflow sees a version without a tag,
runs ruff, mypy and pytest, builds the wheel, and then creates the `vX.Y.Z` tag
and GitHub release with the files attached. It also publishes to PyPI if
`PUBLISH_TO_PYPI` is enabled. Nobody pushes tags by hand.
