class LMSError(Exception):
    """Base exception for all LMS client errors."""

    def __init__(self, message: str, status_code: int | None = None, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(status_code={self.status_code}, message={str(self)})"


class LMSAuthError(LMSError):
    """Raised on 401 — invalid or missing HMAC signature."""


class LMSRateLimitError(LMSError):
    """Raised on 429 — service is being throttled by LMS."""


class LMSValidationError(LMSError):
    """Raised on 422 — request payload failed schema validation."""


class LMSConnectionError(LMSError):
    """Raised when the LMS endpoint is unreachable."""