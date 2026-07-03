from __future__ import annotations

import hashlib
import hmac
import time


def generate_signature(secret: str, body: bytes, timestamp: str) -> str:
    """
    HMAC-SHA256 over  timestamp + "." + hex(sha256(body)).

    The LMS server signs the same string on its side:
        message = f"{timestamp}.{sha256(body).hexdigest()}"
        signature = HMAC-SHA256(secret, message)

    Returns the hex digest.
    """
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def current_timestamp() -> str:
    """Unix epoch as a string (seconds precision), same format LMS expects."""
    return str(int(time.time()))