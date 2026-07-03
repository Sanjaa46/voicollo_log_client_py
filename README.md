# lms-logger

Python client for the LMS Log Management System API.  
Handles HMAC-SHA256 request signing, retries, and response parsing — sync and async.

## Installation

Add to `requirements.txt`:

```
git+https://github.com/YOUR_ORG/lms-logger.git@main
```

Or pin to a specific commit for stability:

```
git+https://github.com/YOUR_ORG/lms-logger.git@abc1234
```

Then install:

```bash
pip install -r requirements.txt
```

---

## Quick Start

### Sync (scripts, CLI tools, Django, etc.)

```python
from lms_logger import LMSClient, AppLogRequest, AuditLogRequest, LogLevel

with LMSClient(
    base_url="https://lms.example.gov.mn",
    service_id="your32charhexserviceid",
    secret="your-hmac-secret",
) as client:

    # Single app log
    client.log_app(AppLogRequest(
        service_id=client.service_id,
        level=LogLevel.INFO,
        message="Service started",
        extra={"version": "1.0.0"},
    ))

    # Batch app logs (one HTTP call)
    client.log_app_batch([
        AppLogRequest(service_id=client.service_id, level=LogLevel.DEBUG, message="step 1"),
        AppLogRequest(service_id=client.service_id, level=LogLevel.DEBUG, message="step 2"),
    ])

    # Audit log (immutable — DELETE is blocked by LMS)
    client.log_audit(AuditLogRequest(
        service_id=client.service_id,
        actor_id="user-uuid",
        action="DELETE",
        resource="citizen_record",
        resource_id="42",
    ))
```

### Async (FastAPI, async services)

```python
from lms_logger.client import AsyncLMSClient
from lms_logger import AppLogRequest, LogLevel

async with AsyncLMSClient(
    base_url="https://lms.example.gov.mn",
    service_id="your32charhexserviceid",
    secret="your-hmac-secret",
) as client:
    await client.log_app(AppLogRequest(
        service_id=client.service_id,
        level=LogLevel.ERROR,
        message="Payment failed",
        extra={"order_id": "ord_999"},
    ))
```

### FastAPI lifespan pattern

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from lms_logger.client import AsyncLMSClient

lms: AsyncLMSClient | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global lms
    lms = AsyncLMSClient(
        base_url=settings.LMS_URL,
        service_id=settings.LMS_SERVICE_ID,
        secret=settings.LMS_SECRET,
    )
    yield
    await lms.close()

app = FastAPI(lifespan=lifespan)
```

---

## Error Handling

```python
from lms_logger.exceptions import LMSAuthError, LMSRateLimitError, LMSValidationError, LMSConnectionError

try:
    client.log_app(...)
except LMSAuthError:
    # Wrong secret or clock drift > tolerance
    ...
except LMSRateLimitError:
    # Back off — do NOT retry immediately
    ...
except LMSValidationError as e:
    # Your payload is malformed — e.response_body has details
    ...
except LMSConnectionError:
    # LMS is down after max_retries attempts
    ...
```

---

## HMAC Signing

The client signs every request with:

```
message = f"{unix_timestamp}.{sha256(body).hexdigest()}"
x-signature = HMAC-SHA256(secret, message)
```

Your LMS server must verify the same string. The secret never leaves your environment.

---

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `base_url` | required | LMS API base URL |
| `service_id` | required | Your registered service UUID (32 hex chars) |
| `secret` | required | HMAC shared secret |
| `timeout` | `10.0` | HTTP timeout in seconds |
| `max_retries` | `3` | Retries on network error (never retries 401/422/429) |