"""Helpers for resolving and validating Iglu self-describing schemas."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jsonschema_rs import Draft4Validator, ValidationError

from evnt.config import settings

IGLU_URI_RE = re.compile(
    r"^iglu:(?P<vendor>[A-Za-z0-9-_.]+)/"
    r"(?P<name>[A-Za-z0-9-_]+)/"
    r"(?P<format>[A-Za-z0-9-_]+)/"
    r"(?P<version>[0-9]+-[0-9]+-[0-9]+)$",
)
# Real schema URIs are well under 100 characters. Longer ones are tracker
# input that is not an Iglu URI, and they are rejected before reaching the
# caches below, which would otherwise pin up to 1024 arbitrarily large strings.
MAX_IGLU_URI_CHARS = 256
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
    invalid_data: bool = False
    """The schema loaded and the data failed it, as opposed to a schema the
    collector could not load: only this is a problem of the event itself."""


def _format_validation_error(error: ValidationError) -> str:
    """Format a jsonschema validation error with a stable payload path."""

    location = "$"
    for part in error.instance_path:
        location += f"[{part}]" if isinstance(part, int) else f".{part}"

    if location == "$":
        return str(error.message)
    return f"{error.message} at {location}"


def resolve_iglu_schema_path(schema_uri: str) -> Path | None:
    """Translate a full Iglu URI into a local schema path."""

    if not isinstance(schema_uri, str) or len(schema_uri) > MAX_IGLU_URI_CHARS:
        return None

    relative_path = _relative_schema_path(schema_uri)
    if relative_path is None:
        return None
    return IGLU_SCHEMAS_DIR / relative_path


@lru_cache(maxsize=1024)
def _relative_schema_path(schema_uri: str) -> Path | None:
    """Parse an Iglu URI once; this runs for every context of every event."""

    match = IGLU_URI_RE.fullmatch(schema_uri)
    if match is None:
        return None
    if match.group("format") != "jsonschema":
        return None

    return Path(
        match.group("vendor"),
        match.group("name"),
        "jsonschema",
        match.group("version"),
    )


def schema_key(schema_uri: str) -> tuple[str, str] | None:
    """Split an Iglu URI into its ``vendor/name`` key and its version.

    ``None`` for anything that is not ``iglu:vendor/name/format/M-R-A``. Any
    format is accepted: only the key and the version are needed here.
    """

    if len(schema_uri) > MAX_IGLU_URI_CHARS:
        return None
    return _schema_key(schema_uri)


@lru_cache(maxsize=1024)
def _schema_key(schema_uri: str) -> tuple[str, str] | None:
    # Cached: an event carries several entities, and the same handful of
    # schemas repeats across every event.
    match = IGLU_URI_RE.fullmatch(schema_uri)
    if match is None:
        return None
    return f"{match.group('vendor')}/{match.group('name')}", match.group("version")


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

    # The constructor also checks the schema against the Draft 4 meta-schema.
    # ``format`` stays an annotation, as it was with the previous validator.
    return Draft4Validator(schema, validate_formats=False), schema_path


def clear_iglu_caches() -> None:
    """Reset cached validators for tests."""

    _load_validator.cache_clear()
    _unavailable.cache_clear()
    _relative_schema_path.cache_clear()
    _schema_key.cache_clear()


@lru_cache(maxsize=1024)
def _unavailable(schema_uri: str) -> ValidationResult | None:
    """Why a schema has no file to load, or ``None`` when it has one.

    Most custom schemas are not in the local mirror, and every event carrying
    one would otherwise pay a ``stat`` and a raised exception per entity. Kept
    apart from the validator cache, so URIs made up by a client can only evict
    each other, never a compiled validator.
    """

    schema_path = resolve_iglu_schema_path(schema_uri)
    if schema_path is None:
        return ValidationResult(status="skipped")
    if not schema_path.is_file():
        return ValidationResult(
            status="warning",
            schema_path=schema_path,
            error="schema file not found",
        )
    return None


def _load(schema_uri: str) -> tuple[Draft4Validator, Path] | ValidationResult:
    """The compiled validator of a schema, or why there is none."""

    if not isinstance(schema_uri, str) or len(schema_uri) > MAX_IGLU_URI_CHARS:
        return ValidationResult(status="skipped")
    if (unavailable := _unavailable(schema_uri)) is not None:
        return unavailable

    try:
        return _load_validator(schema_uri)
    except FileNotFoundError:
        error = "schema file not found"
    except json.JSONDecodeError as exc:
        error = f"schema JSON decode failed: {exc}"
    except OSError as exc:
        error = f"schema file read failed: {exc}"
    except ValidationError as exc:
        error = f"schema compilation failed: {exc.message}"
    except ValueError as exc:
        error = f"schema compilation failed: {exc}"

    return ValidationResult(
        status="warning",
        schema_path=resolve_iglu_schema_path(schema_uri),
        error=error,
    )


def prepare_iglu_schema(schema_uri: str) -> ValidationResult:
    """Resolve and cache a schema validator without validating payload data."""

    loaded = _load(schema_uri)
    if isinstance(loaded, ValidationResult):
        return loaded
    return ValidationResult(status="ok", schema_path=loaded[1])


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

    loaded = _load(schema_uri)
    if isinstance(loaded, ValidationResult):
        return loaded

    validator, cached_path = loaded
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
        invalid_data=True,
    )
