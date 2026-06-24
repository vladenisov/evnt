"""
Constants and magic values for evnt.

This module centralizes all constant values used throughout the application
to improve maintainability and reduce magic strings/numbers.
"""

import base64
import tempfile
from pathlib import Path
from typing import Final

# Application metadata
APP_NAME: Final[str] = "evnt"
APP_SLUG: Final[str] = "evnt"
APP_VERSION: Final[str] = "0.6.0"

# Tracking pixel (1x1 transparent GIF)
TRACKING_PIXEL: Final[bytes] = base64.b64decode(
    b"R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==",
)

# Content types
CONTENT_TYPE_GIF: Final[str] = "image/gif"
CONTENT_TYPE_JSON: Final[str] = "application/json"
CONTENT_TYPE_OCTET_STREAM: Final[str] = "application/octet-stream"

# Timeouts (in seconds)
DEFAULT_PROXY_TIMEOUT: Final[float] = 10.0
DEFAULT_DB_CONNECT_TIMEOUT: Final[int] = 10

# Database defaults
DEFAULT_DATABASE_NAME: Final[str] = "evnt"
DEFAULT_TABLE_GROUP: Final[str] = "evnt"

# Security headers
SECURITY_HEADERS: Final[dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
HSTS_HEADER: Final[str] = "max-age=31536000; includeSubDomains"

# Endpoint paths
DEFAULT_POST_ENDPOINT: Final[str] = "/tracker"
DEFAULT_GET_ENDPOINT: Final[str] = "/i"
DEFAULT_PROXY_ENDPOINT: Final[str] = "/proxy"
DEFAULT_SENDGRID_ENDPOINT: Final[str] = "/sendgrid"
DEFAULT_METRICS_PATH: Final[str] = "/metrics/"

# ClickHouse defaults
DEFAULT_CLICKHOUSE_HOST: Final[str] = "clickhouse"
DEFAULT_CLICKHOUSE_PORT: Final[int] = 8123
DEFAULT_CLICKHOUSE_INTERFACE: Final[str] = "http"
DEFAULT_CLICKHOUSE_USERNAME: Final[str] = "default"
DEFAULT_CLICKHOUSE_DATABASE: Final[str] = "default"
DEFAULT_CLICKHOUSE_STARTUP_TIMEOUT_SECONDS: Final[int] = 60
DEFAULT_CLICKHOUSE_STARTUP_RETRY_INTERVAL_MS: Final[int] = 1000
DEFAULT_INGEST_MODE: Final[str] = "direct"
DEFAULT_RABBITMQ_HOST: Final[str] = "rabbitmq"
DEFAULT_RABBITMQ_PORT: Final[int] = 5672
DEFAULT_RABBITMQ_QUEUE_NAME: Final[str] = "evnt.ingest"
DEFAULT_RABBITMQ_BATCH_SIZE: Final[int] = 500
DEFAULT_RABBITMQ_PREFETCH_COUNT: Final[int] = DEFAULT_RABBITMQ_BATCH_SIZE
DEFAULT_RABBITMQ_BATCH_TIMEOUT_MS: Final[int] = 1000
DEFAULT_RABBITMQ_RETRY_DELAY_MS: Final[int] = 1000
DEFAULT_RABBITMQ_CONNECT_TIMEOUT_SECONDS: Final[int] = 5
DEFAULT_RABBITMQ_STARTUP_TIMEOUT_SECONDS: Final[int] = 60
DEFAULT_RABBITMQ_STARTUP_RETRY_INTERVAL_MS: Final[int] = 1000

# Worker liveness contract
# The worker writes the wall-clock time of its last successful flush to this
# file; the healthcheck considers the worker dead if the file is missing or the
# recorded timestamp is older than the staleness threshold. This threshold must
# stay strictly greater than the worker's MAX_BACKOFF_SECONDS so a sustained
# backend outage (which makes the worker sleep up to one full backoff between
# liveness writes) cannot be misread as a dead worker.
WORKER_LIVENESS_PATH: Final[Path] = Path(tempfile.gettempdir()) / "evnt-worker.alive"
WORKER_LIVENESS_STALE_SECONDS: Final[int] = 120
# How often the worker proactively refreshes its liveness file, independent of
# message flow / batch timeout / backoff. Kept well below the stale threshold so
# an idle worker (even with a large EVNT_INGEST__RABBITMQ__BATCH_TIMEOUT_MS) is
# never mistaken for dead between writes.
WORKER_HEARTBEAT_SECONDS: Final[int] = WORKER_LIVENESS_STALE_SECONDS // 3

# Async insert settings for ClickHouse
CLICKHOUSE_ASYNC_SETTINGS: Final[dict[str, int]] = {
    "async_insert": 1,
    "wait_for_async_insert": 0,
}

# Log levels
LOG_LEVEL_DEBUG: Final[str] = "DEBUG"
LOG_LEVEL_INFO: Final[str] = "INFO"
LOG_LEVEL_WARNING: Final[str] = "WARNING"
LOG_LEVEL_ERROR: Final[str] = "ERROR"

# Environment names
ENV_PRODUCTION: Final[str] = "production"
ENV_DEVELOPMENT: Final[str] = "development"
SENTRY_ENV_PROD: Final[str] = "prod"
SENTRY_ENV_DEV: Final[str] = "dev"
