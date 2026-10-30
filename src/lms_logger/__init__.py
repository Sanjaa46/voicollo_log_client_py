"""Python client for the Voicollo Log Management System.

from lms_logger import LMSClient

lms = LMSClient(base_url=..., secret=..., source_service="billing-api",
                environment="production")
lms.info("Payment captured", context={"order_id": 42})
"""

from ._version import __version__
from .client import AsyncLMSClient, LMSClient
from .exceptions import (
    LMSAuthError,
    LMSConfigError,
    LMSConflictError,
    LMSConnectionError,
    LMSError,
    LMSNotFoundError,
    LMSPermissionError,
    LMSQueueFullError,
    LMSRateLimitError,
    LMSServerError,
    LMSValidationError,
)
from .handler import LMSHandler
from .models import (
    ActorType,
    AppLogRequest,
    AppLogResponse,
    AuditLogRequest,
    AuditLogResponse,
    BatchAppLogResponse,
    BatchLogResult,
    IngestResponse,
    LogLevel,
    Outcome,
    OutcomeType,
)

VERSION = __version__

__all__ = [
    "VERSION",
    "ActorType",
    "AppLogRequest",
    "AppLogResponse",
    "AsyncLMSClient",
    "AuditLogRequest",
    "AuditLogResponse",
    "BatchAppLogResponse",
    "BatchLogResult",
    "IngestResponse",
    "LMSAuthError",
    "LMSClient",
    "LMSConfigError",
    "LMSConflictError",
    "LMSConnectionError",
    "LMSError",
    "LMSHandler",
    "LMSNotFoundError",
    "LMSPermissionError",
    "LMSQueueFullError",
    "LMSRateLimitError",
    "LMSServerError",
    "LMSValidationError",
    "LogLevel",
    "Outcome",
    "OutcomeType",
    "__version__",
]
