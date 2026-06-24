"""
Configuration management for evnt.

This module defines all configuration models using Pydantic for validation.
Settings can be configured via environment variables with the EVNT_ prefix.
"""

import os
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import (
    APP_SLUG,
    DEFAULT_CLICKHOUSE_DATABASE,
    DEFAULT_CLICKHOUSE_HOST,
    DEFAULT_CLICKHOUSE_INTERFACE,
    DEFAULT_CLICKHOUSE_PORT,
    DEFAULT_CLICKHOUSE_STARTUP_RETRY_INTERVAL_MS,
    DEFAULT_CLICKHOUSE_STARTUP_TIMEOUT_SECONDS,
    DEFAULT_CLICKHOUSE_USERNAME,
    DEFAULT_DATABASE_NAME,
    DEFAULT_DB_CONNECT_TIMEOUT,
    DEFAULT_GET_ENDPOINT,
    DEFAULT_INGEST_MODE,
    DEFAULT_METRICS_PATH,
    DEFAULT_POST_ENDPOINT,
    DEFAULT_PROXY_ENDPOINT,
    DEFAULT_RABBITMQ_BATCH_SIZE,
    DEFAULT_RABBITMQ_BATCH_TIMEOUT_MS,
    DEFAULT_RABBITMQ_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_RABBITMQ_HOST,
    DEFAULT_RABBITMQ_PORT,
    DEFAULT_RABBITMQ_PREFETCH_COUNT,
    DEFAULT_RABBITMQ_QUEUE_NAME,
    DEFAULT_RABBITMQ_RETRY_DELAY_MS,
    DEFAULT_RABBITMQ_STARTUP_RETRY_INTERVAL_MS,
    DEFAULT_RABBITMQ_STARTUP_TIMEOUT_SECONDS,
    DEFAULT_SENDGRID_ENDPOINT,
    ENV_DEVELOPMENT,
    ENV_PRODUCTION,
    LOG_LEVEL_WARNING,
    SENTRY_ENV_DEV,
    SENTRY_ENV_PROD,
)


class SnowplowSchemas(BaseModel):
    """Schema identifiers for Snowplow events."""

    user_data: str = "dev.snowplow.simple/user_data"
    page_data: str = "dev.snowplow.simple/page_data"
    screen_data: str = "dev.snowplow.simple/screen_data"
    ad_data: str = "dev.snowplow.simple/ad_data"
    u2s_data: str = "dev.snowplow.simple/u2s_data"


class SnowplowEndpoints(BaseModel):
    """Endpoint paths for Snowplow collectors."""

    post_endpoint: str = DEFAULT_POST_ENDPOINT
    get_endpoint: str = DEFAULT_GET_ENDPOINT
    proxy_endpoint: str = DEFAULT_PROXY_ENDPOINT
    sendgrid_endpoint: str = DEFAULT_SENDGRID_ENDPOINT


class Snowplow(BaseModel):
    """Snowplow-specific configuration."""

    schemas: SnowplowSchemas = SnowplowSchemas()
    endpoints: SnowplowEndpoints = SnowplowEndpoints()
    user_ip_header: str = "X-Forwarded-For"

    @field_validator("user_ip_header")
    @classmethod
    def validate_user_ip_header(cls, value: str) -> str:
        """Ensure the user IP header name is not empty."""
        header_name = value.strip()
        if not header_name:
            raise ValueError("user_ip_header must not be empty")
        return header_name


_VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


class LoggingConfig(BaseModel):
    """Logging configuration."""

    json_format: bool = False
    level: str = LOG_LEVEL_WARNING

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        """Ensure log level is uppercase and valid."""
        upper_v = v.upper()
        if upper_v not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"Invalid log level: {v}. Must be one of {_VALID_LOG_LEVELS}"
            )
        return upper_v


class SecurityConfig(BaseModel):
    """Security-related configuration."""

    disable_docs: bool = True
    trusted_hosts: list[str] = ["*"]
    enable_https_redirect: bool = False
    trust_proxy_headers: bool = True
    cors_allowed_origins: list[str] = ["*"]
    cors_allow_credentials: bool = False

    @field_validator("cors_allowed_origins")
    @classmethod
    def normalize_cors_allowed_origins(cls, values: list[str]) -> list[str]:
        """Normalize configured CORS origins for exact browser Origin matching."""
        normalized: list[str] = []

        for value in values:
            origin = value.strip()
            if not origin:
                raise ValueError("cors_allowed_origins entries must not be empty")

            if origin == "*":
                normalized.append(origin)
                continue

            parsed = urlsplit(origin.rstrip("/"))
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "cors_allowed_origins entries must be bare HTTP(S) origins",
                )

            normalized.append(f"{parsed.scheme.lower()}://{parsed.netloc.lower()}")

        if "*" in normalized and len(normalized) > 1:
            raise ValueError(
                "cors_allowed_origins cannot mix '*' with explicit origins",
            )

        return normalized

    @model_validator(mode="after")
    def validate_cors_credentials(self) -> SecurityConfig:
        """Reject combining credentialed CORS with wildcard origins."""

        if self.cors_allowed_origins == ["*"] and self.cors_allow_credentials:
            raise ValueError(
                "cannot combine credentials with wildcard origins",
            )
        return self


class ElasticAPMConfig(BaseModel):
    """Elastic APM configuration."""

    enabled: bool = False
    service_name: str = APP_SLUG
    server_url: str | None = None


class PrometheusConfig(BaseModel):
    """Prometheus metrics configuration."""

    enabled: bool = False
    metrics_path: str = DEFAULT_METRICS_PATH


class SentryConfig(BaseModel):
    """Sentry error tracking configuration."""

    enabled: bool = False
    dsn: str | None = None
    traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    environment: str = (
        SENTRY_ENV_PROD
        if os.getenv("EVNT_ENV", ENV_DEVELOPMENT) == ENV_PRODUCTION
        else SENTRY_ENV_DEV
    )


class ProxyConfig(BaseModel):
    """Proxy configuration for external services."""

    domains: list[str] = ["google-analytics.com", "www.googletagmanager.com"]
    paths: list[str] = ["analytics.js", "gtm.js"]
    # Outbound ports the proxy is allowed to reach on an allowlisted host.
    # The hostname allowlist alone does not constrain the port, so this keeps
    # the proxy on standard web ports by default while letting operators opt
    # into additional ports for hosts they explicitly trust. A target with no
    # explicit port (the scheme default) is always permitted.
    allowed_ports: list[int] = [80, 443]


class PerformanceConfig(BaseModel):
    """Performance tuning configuration."""

    max_concurrent_connections: int = Field(default=100, gt=0)
    db_pool_size: int = Field(default=5, gt=0)
    db_pool_overflow: int = Field(default=10, gt=0)
    healthcheck_cache_ttl_seconds: float = Field(default=2.0, ge=0.0)
    enable_access_log: bool = True
    access_log_excluded_paths: list[str] = []
    enable_brotli: bool = True
    brotli_excluded_paths: list[str] = []


class ClickHouseConnection(BaseModel):
    """ClickHouse connection parameters."""

    interface: str = DEFAULT_CLICKHOUSE_INTERFACE
    host: str = DEFAULT_CLICKHOUSE_HOST
    port: int = DEFAULT_CLICKHOUSE_PORT
    username: str = DEFAULT_CLICKHOUSE_USERNAME
    database: str = DEFAULT_CLICKHOUSE_DATABASE
    password: SecretStr = SecretStr("password")
    connect_timeout: int = DEFAULT_DB_CONNECT_TIMEOUT

    def as_client_kwargs(self) -> dict[str, Any]:
        """Return connection kwargs with the password unwrapped for the client."""
        data = self.model_dump()
        data["password"] = self.password.get_secret_value()
        return data


class ClickHouseConfiguration(BaseModel):
    """ClickHouse table configuration."""

    database: str = DEFAULT_DATABASE_NAME
    cluster_name: str = ""

    # Can be overridden via environment variables such as:
    # EVNT_CLICKHOUSE__CONFIGURATION__TABLES__EVNT__LOCAL__ENGINE=
    #   "ReplacingMergeTree()"
    tables: dict[str, Any] = {
        "evnt": {
            "enabled": True,
            "local": {
                "name": "local",
                "engine": "MergeTree()",
                "partition_by": "toYYYYMM(time)",
                "order_by": ", ".join(
                    [
                        "app",
                        "platform",
                        "app_id",
                        "event_type",
                        "toDate(time)",
                        "event.category",
                        "event.action",
                        "page",
                        "device_id",
                        "cityHash64(device_id)",
                        "session_id",
                        "time",
                    ]
                ),
                "sample_by": "cityHash64(device_id)",
                "settings": "index_granularity = 8192",
            },
            "distributed": {
                "name": "distributed",
                "sample_by": "cityHash64(device_id)",
            },
        },
    }


class ClickHouseConfig(BaseModel):
    """Complete ClickHouse configuration."""

    connection: ClickHouseConnection = ClickHouseConnection()
    configuration: ClickHouseConfiguration = ClickHouseConfiguration()
    tables: dict[str, Any] = {}
    startup_timeout_seconds: int = Field(
        default=DEFAULT_CLICKHOUSE_STARTUP_TIMEOUT_SECONDS,
        gt=0,
    )
    startup_retry_interval_ms: int = Field(
        default=DEFAULT_CLICKHOUSE_STARTUP_RETRY_INTERVAL_MS,
        gt=0,
    )


class DirectInsertConfig(BaseModel):
    """Direct ClickHouse insert settings."""

    async_insert: bool = True
    wait_for_async_insert: bool = False


class RabbitMQConfig(BaseModel):
    """RabbitMQ-backed ingest settings."""

    host: str = DEFAULT_RABBITMQ_HOST
    port: int = Field(default=DEFAULT_RABBITMQ_PORT, gt=0)
    username: str = "guest"
    password: SecretStr = SecretStr("guest")
    virtualhost: str = "/"
    queue_name: str = DEFAULT_RABBITMQ_QUEUE_NAME
    failed_queue_name: str | None = None
    prefetch_count: int = Field(default=DEFAULT_RABBITMQ_PREFETCH_COUNT, gt=0)
    batch_size: int = Field(default=DEFAULT_RABBITMQ_BATCH_SIZE, gt=0)
    batch_timeout_ms: int = Field(default=DEFAULT_RABBITMQ_BATCH_TIMEOUT_MS, gt=0)
    retry_delay_ms: int = Field(default=DEFAULT_RABBITMQ_RETRY_DELAY_MS, gt=0)
    connect_timeout_seconds: int = Field(
        default=DEFAULT_RABBITMQ_CONNECT_TIMEOUT_SECONDS,
        gt=0,
    )
    startup_timeout_seconds: int = Field(
        default=DEFAULT_RABBITMQ_STARTUP_TIMEOUT_SECONDS,
        gt=0,
    )
    startup_retry_interval_ms: int = Field(
        default=DEFAULT_RABBITMQ_STARTUP_RETRY_INTERVAL_MS,
        gt=0,
    )

    @model_validator(mode="after")
    def validate_failed_queue_name(self) -> RabbitMQConfig:
        """Ensure the failed queue is distinct from the main queue."""

        if self.failed_queue_name and self.failed_queue_name == self.queue_name:
            raise ValueError("failed_queue_name must differ from queue_name")
        return self

    @property
    def resolved_failed_queue_name(self) -> str:
        """Return the configured failed queue or a derived default."""

        return self.failed_queue_name or f"{self.queue_name}.failed"


class IngestConfig(BaseModel):
    """Ingest pipeline settings."""

    mode: Literal["direct", "rabbitmq"] = DEFAULT_INGEST_MODE
    direct: DirectInsertConfig = DirectInsertConfig()
    rabbitmq: RabbitMQConfig = RabbitMQConfig()


class CommonConfig(BaseModel):
    """Common application configuration."""

    demo: bool = False
    debug: bool = False
    service_name: str = APP_SLUG
    hostname: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    snowplow: Snowplow = Snowplow()


class Settings(BaseSettings):
    """Main application settings populated from file + env vars.

    Nested models are supported via double underscore environment variable keys
    (e.g. ``EVNT_LOGGING__LEVEL=DEBUG``).

    Example:
        >>> from core.config import settings
        >>> settings.logging.level
        'WARNING'
    """

    model_config = SettingsConfigDict(
        env_prefix="EVNT_",
        case_sensitive=False,
        env_nested_delimiter="__",
    )

    logging: LoggingConfig = LoggingConfig()
    security: SecurityConfig = SecurityConfig()
    elastic_apm: ElasticAPMConfig = ElasticAPMConfig()
    prometheus: PrometheusConfig = PrometheusConfig()
    sentry: SentryConfig = SentryConfig()
    proxy: ProxyConfig = ProxyConfig()
    performance: PerformanceConfig = PerformanceConfig()
    common: CommonConfig = CommonConfig()
    clickhouse: ClickHouseConfig = ClickHouseConfig()
    ingest: IngestConfig = IngestConfig()

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return os.getenv("EVNT_ENV", ENV_DEVELOPMENT) == ENV_PRODUCTION

    @property
    def is_debug(self) -> bool:
        """Check if debug mode is enabled."""
        return self.common.debug


settings = Settings()
