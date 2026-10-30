"""Request signing — must match the server's ``verify_service_signature``.

X-Timestamp: unix seconds
X-Signature: hex(HMAC_SHA256(secret, f"{timestamp}." + raw_body))
"""

from __future__ import annotations

import hashlib
import hmac
import time


def generate_signature(secret: str, body: bytes, timestamp: str) -> str:
    """Return the hex HMAC-SHA256 of ``f"{timestamp}." + body``."""
    message = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def current_timestamp() -> str:
    """Unix epoch seconds as a string."""
    return str(int(time.time()))


def signed_headers(secret: str, body: bytes, timestamp: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Timestamp": timestamp,
        "X-Signature": generate_signature(secret, body, timestamp),
    }
