# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [SemVer](https://semver.org/). The version lives in
`src/lms_logger/_version.py`; releases are git tags `vX.Y.Z`.

## [Unreleased]

### Changed
- Releases are automatic: merging a version bump into `main` creates the
  `vX.Y.Z` tag and GitHub release. Tags no longer need to be pushed by hand.
- The release workflow now runs ruff, mypy and pytest before building.

## [1.0.0] - 2026-10-30

Requires LMS server **2.0.0** or newer for the documented error codes (older
servers work, with less specific errors).

### Breaking
- All constructor arguments after `base_url` are keyword-only:
  `LMSClient(base_url, secret=..., source_service=..., environment=...)`.
- `max_retries` now counts *retries* (default 3 → up to 4 attempts).
- `AuditLogRequest.actor_id` is required and `ip_address` optional, matching
  the server (they were the other way round).
- `AppLogRequest.trace_id` is optional (it was required).
- Requires Python ≥ 3.10.

### Added
- `source_service` / `environment` defaults on the client; per-call overrides.
- `info()/debug()/warn()/error()/fatal()/log()` and `audit()` helpers.
- Buffered mode (`buffered=True`): background batching, `flush()`, `close()`
  drains, `on_error` callback; logging never raises into the application.
- `LMSHandler` / `client.handler()` for the stdlib `logging` module (ignores
  the HTTP stack's own records to avoid feedback loops).
- Exceptions for every status: `LMSPermissionError` (403), `LMSNotFoundError`,
  `LMSConflictError` (409), `LMSServerError` (5xx), `LMSConfigError`,
  `LMSQueueFullError`; all carry `error_code` and `request_id`.
- `AsyncLMSClient`, `ActorType`, `Outcome` exported from the package root.
- `__version__`, `User-Agent: lms-logger-python/<version>`, `py.typed`.

### Fixed
- Retries reused the first signature and were rejected as expired/replayed;
  every attempt is now signed with a fresh timestamp.
- 5xx and 429 responses were not retried; `Retry-After` is honoured.
- Naive datetimes are converted from local time to UTC instead of being sent
  without an offset.
- Payloads are serialised in JSON mode (enums/datetimes) consistently.

## [0.1.0] - 2025-06-11

Initial version.
