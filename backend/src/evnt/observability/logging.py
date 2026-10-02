"""Logging integration using fastapi-structlog.

This replaces the previous custom Structlog configuration with the
`fastapi-structlog` helper utilities so we can rely on its battle-tested
middleware, processors and optional destinations (console / JSON / file / syslog / DB).
"""

import structlog
from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi_structlog import LogSettings, setup_logger
from fastapi_structlog.settings import DBSettings, LogType, SysLogSettings


def init_logging(enable_json_logs: bool, log_level: str) -> None:
    """Initialize logging via fastapi-structlog.

    Args:
        enable_json_logs: Whether logs should be emitted in JSON format.
        log_level: Base log level (string like INFO, DEBUG).
    """

    settings = LogSettings(
        logger="evnt",
        log_level=log_level.upper(),
        json_logs=enable_json_logs,
        traceback_as_str=True,
        # Only console output for now. JSON vs pretty decided by json_logs flag.
        types=[LogType.CONSOLE],
        syslog=SysLogSettings(),
        db=DBSettings(),
    )

    # Configure structlog / stdlib logging.
    setup_logger(settings)

    # Bind default contextual information (can be enriched in middlewares/routes)
    structlog.contextvars.bind_contextvars(service="evnt")


def _parse_content_length(value: str | None) -> int | None:
    """Parse and validate Content-Length header value."""
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _get_body_length(body: object) -> int | None:
    """Get request body length in bytes for logging."""
    if body is None:
        return None
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, memoryview):
        return len(body.tobytes())
    return len(str(body).encode("utf-8", errors="ignore"))


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Unified validation error handler using the configured structlog logger."""
    content_length = _parse_content_length(request.headers.get("content-length"))
    body_length = _get_body_length(exc.body)

    error_data = {
        "detail": exc.errors(),
        "body": exc.body,
        "content_length": content_length,
        "body_length": body_length,
        "body_length_matches_content_length": (
            content_length == body_length
            if content_length is not None and body_length is not None
            else None
        ),
    }
    logger = structlog.get_logger()
    logger.error("Validation error", **error_data)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder(error_data),
    )
