"""Request/response models. Validation mirrors the LMS server, so bad entries fail
locally (``pydantic.ValidationError``) instead of costing a round-trip."""

from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    WARNING = "WARN"  # alias, matches the stdlib name
    ERROR = "ERROR"
    FATAL = "FATAL"
    CRITICAL = "FATAL"  # alias


class ActorType(str, Enum):
    USER = "USER"
    SERVICE = "SERVICE"
    SYSTEM = "SYSTEM"


class Outcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PARTIAL = "PARTIAL"


OutcomeType = Outcome  # backwards-compatible name

_LEVEL_ALIASES = {"WARNING": "WARN", "CRITICAL": "FATAL", "ERR": "ERROR", "TRACE": "DEBUG"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    """Naive datetimes are interpreted as *local* time (what ``datetime.now()`` returns)."""
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return to_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Entry(BaseModel):
    model_config = ConfigDict(use_enum_values=False, str_strip_whitespace=False, extra="forbid")

    source_service: str | None = Field(
        default=None, min_length=1, max_length=64, description="Defaults to the client's value."
    )
    environment: str | None = Field(
        default=None, min_length=1, max_length=32, description="Defaults to the client's value."
    )

    @field_validator("source_service", "environment", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class AppLogRequest(_Entry):
    level: LogLevel
    message: str = Field(min_length=1, max_length=65_000)
    created_at: datetime = Field(default_factory=utcnow)
    trace_id: str | None = Field(default=None, max_length=64)
    context_json: dict[str, Any] | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _level(cls, value: Any) -> Any:
        if isinstance(value, str) and not isinstance(value, LogLevel):
            upper = value.strip().upper()
            return _LEVEL_ALIASES.get(upper, upper)
        return value

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value

    @field_serializer("created_at")
    def _ser_time(self, value: datetime) -> str:
        return _iso(value)

    @field_serializer("level")
    def _ser_level(self, value: LogLevel) -> str:
        return value.value


class AuditLogRequest(_Entry):
    actor_id: str = Field(min_length=1, max_length=128)
    actor_type: ActorType
    action: str = Field(min_length=1, max_length=128, description="VERB_NOUN, e.g. USER_LOGIN")
    target_resource: str = Field(min_length=1, max_length=256)
    outcome: Outcome
    event_time: datetime = Field(default_factory=utcnow)
    ip_address: str | None = Field(default=None, max_length=45)
    detail_json: dict[str, Any] | None = None

    @field_validator("actor_type", "outcome", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        if isinstance(value, str) and not isinstance(value, Enum):
            return value.strip().upper()
        return value

    @field_validator("action")
    @classmethod
    def _verb_noun(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("action must be VERB_NOUN without spaces, e.g. USER_LOGIN")
        return value.upper()

    @field_validator("ip_address", mode="before")
    @classmethod
    def _ip(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return str(ipaddress.ip_address(str(value).strip()))

    @field_serializer("event_time")
    def _ser_time(self, value: datetime) -> str:
        return _iso(value)

    @field_serializer("actor_type", "outcome")
    def _ser_enum(self, value: Enum) -> str:
        return str(value.value)


class IngestResponse(BaseModel):
    log_id: int
    ingested_at: datetime


AppLogResponse = IngestResponse
AuditLogResponse = IngestResponse


class BatchLogResult(BaseModel):
    index: int
    success: bool
    log_id: int | None = None
    error_code: str | None = None
    message: str | None = None


class BatchAppLogRequest(BaseModel):
    logs: list[AppLogRequest] = Field(min_length=1, max_length=100)


class BatchAppLogResponse(BaseModel):
    results: list[BatchLogResult]
    total: int
    succeeded: int
    failed: int

    @property
    def failures(self) -> list[BatchLogResult]:
        return [r for r in self.results if not r.success]
