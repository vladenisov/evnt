"""Helpers for resolving and validating Iglu self-describing schemas."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from evnt.config import settings
from jsonschema import Draft4Validator
from jsonschema.exceptions import SchemaError, ValidationError

IGLU_URI_RE = re.compile(
    r"^iglu:(?P<vendor>[A-Za-z0-9-_.]+)/"
    r"(?P<name>[A-Za-z0-9-_]+)/"
    r"(?P<format>[A-Za-z0-9-_]+)/"
    r"(?P<version>[0-9]+-[0-9]+-[0-9]+)$",
)
IGLU_SCHEMAS_DIR: Path = settings.common.snowplow.iglu_schemas_dir
KNOWN_IGLU_SCHEMA_URIS = (
    "iglu:org.w3/PerformanceTiming/jsonschema/1-0-0",
    "iglu:org.ietf/http_client_hints/jsonschema/1-0-0",
    "iglu:com.google.analytics/cookies/jsonschema/1-0-0",
    "iglu:com.google.ga4/cookies/jsonschema/1-0-0",
    "iglu:com.snowplowanalytics.snowplow/web_page/jsonschema/1-0-0",
    "iglu:dev.amp.snowplow/amp_session/jsonschema/1-0-0",
    "iglu:dev.amp.snowplow/amp_id/jsonschema/1-0-0",
    "iglu:dev.amp.snowplow/amp_web_page/jsonschema/1-0-0",
    "iglu:com.snowplowanalytics.snowplow/mobile_context/jsonschema/1-0-3",
    "iglu:com.snowplowanalytics.mobile/application/jsonschema/1-0-0",
    "iglu:com.snowplowanalytics.snowplow/client_session/jsonschema/1-0-2",
    "iglu:com.snowplowanalytics.mobile/screen/jsonschema/1-0-0",
    "iglu:com.snowplowanalytics.snowplow/browser_context/jsonschema/2-0-0",
    "iglu:com.snowplowanalytics.snowplow/geolocation_context/jsonschema/1-1-0",
    "iglu:com.snowplowanalytics.mobile/screen_summary/jsonschema/1-0-0",
    "iglu:com.snowplowanalytics.mobile/application_lifecycle/jsonschema/1-0-0",
    "iglu:com.android.installreferrer.api/referrer_details/jsonschema/1-0-0",
    "iglu:org.w3/PerformanceNavigationTiming/jsonschema/1-0-0",
    "iglu:com.snowplowanalytics.mobile/deep_link/jsonschema/1-0-0",
    "iglu:com.snowplowanalytics.mobile/deep_link_received/jsonschema/1-0-0",
    "iglu:com.snowplowanalytics.mobile/message_notification/jsonschema/1-0-0",
)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating a payload against an Iglu schema."""

    status: Literal["ok", "skipped", "warning"]
    schema_path: Path | None = None
    error: str | None = None


def _format_validation_error(error: ValidationError) -> str:
    """Format a jsonschema validation error with a stable payload path."""

    location = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            location = f"{location}[{part}]"
        else:
            location = f"{location}.{part}"

    if location == "$":
        return error.message
    return f"{error.message} at {location}"


def resolve_iglu_schema_path(schema_uri: str) -> Path | None:
    """Translate a full Iglu URI into a local schema path."""

    if not isinstance(schema_uri, str):
        return None

    match = IGLU_URI_RE.fullmatch(schema_uri)
    if match is None:
        return None
    if match.group("format") != "jsonschema":
        return None

    return (
        IGLU_SCHEMAS_DIR
        / match.group("vendor")
        / match.group("name")
        / "jsonschema"
        / match.group("version")
    )


@lru_cache(maxsize=256)
def _load_validator(schema_uri: str) -> tuple[Draft4Validator, Path]:
    """Load and compile a validator for a schema URI."""

    schema_path = resolve_iglu_schema_path(schema_uri)
    if schema_path is None:
        raise ValueError("schema URI is not a full Iglu jsonschema URI")
    if not schema_path.is_file():
        raise FileNotFoundError(schema_path)

    with schema_path.open("r", encoding="utf-8") as schema_file:
        schema = json.load(schema_file)

    Draft4Validator.check_schema(schema)
    return Draft4Validator(schema), schema_path


def clear_iglu_caches() -> None:
    """Reset cached validators for tests."""

    _load_validator.cache_clear()


def prepare_iglu_schema(schema_uri: str) -> ValidationResult:
    """Resolve and cache a schema validator without validating payload data."""

    schema_path = resolve_iglu_schema_path(schema_uri)
    if schema_path is None:
        return ValidationResult(status="skipped")

    try:
        _, cached_path = _load_validator(schema_uri)
    except FileNotFoundError:
        error = "schema file not found"
        warning_path = schema_path
    except json.JSONDecodeError as exc:
        error = f"schema JSON decode failed: {exc}"
        warning_path = schema_path
    except OSError as exc:
        error = f"schema file read failed: {exc}"
        warning_path = schema_path
    except (SchemaError, ValueError) as exc:
        error = f"schema compilation failed: {exc}"
        warning_path = schema_path
    else:
        return ValidationResult(status="ok", schema_path=cached_path)

    return ValidationResult(
        status="warning",
        schema_path=warning_path,
        error=error,
    )


def warm_iglu_schema_cache(
    schema_uris: Iterable[str] = KNOWN_IGLU_SCHEMA_URIS,
) -> dict[str, ValidationResult]:
    """Preload and cache validators for the provided schema URIs."""

    results: dict[str, ValidationResult] = {}
    for schema_uri in schema_uris:
        results[schema_uri] = prepare_iglu_schema(schema_uri)
    return results


def validate_iglu_payload(schema_uri: str, data: Any) -> ValidationResult:
    """Validate payload data against a locally resolved Iglu schema."""

    load_result = prepare_iglu_schema(schema_uri)
    if load_result.status != "ok":
        return load_result

    validator, cached_path = _load_validator(schema_uri)
    try:
        validator.validate(data)
    except ValidationError as exc:
        error = _format_validation_error(exc)
        warning_path = cached_path
    else:
        return ValidationResult(status="ok", schema_path=cached_path)

    return ValidationResult(
        status="warning",
        schema_path=warning_path,
        error=error,
    )
