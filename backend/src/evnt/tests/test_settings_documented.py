"""Every setting an operator can set must appear in ``website/docs``."""

import re
from collections.abc import Iterator
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from evnt.config import Settings

# Leaves that are deliberately undocumented. Only for a setting that is not
# user-facing; say why next to each name.
UNDOCUMENTED: frozenset[str] = frozenset()

# Read with os.getenv in config.py, outside the settings model.
OUT_OF_MODEL_ENV_VARS: tuple[str, ...] = ("EVNT_ENV",)


def nested_models(annotation: Any) -> list[type[BaseModel]]:
    """Models whose fields are settable below a field: ``Sub`` or ``Sub | None``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    if get_origin(annotation) in (Union, UnionType):
        return [model for member in get_args(annotation) for model in nested_models(member)]
    return []


def env_names(model: type[BaseModel], prefix: str, delimiter: str) -> Iterator[str]:
    """Yield the env var name of every leaf setting below ``model``."""
    for name, field in model.model_fields.items():
        env_name = f"{prefix}{name.upper()}"
        models = nested_models(field.annotation)
        if not models:
            yield env_name
        for nested in models:
            yield from env_names(nested, f"{env_name}{delimiter}", delimiter)


def settings_env_names(settings_model: type[BaseSettings]) -> list[str]:
    config = settings_model.model_config
    return list(
        env_names(
            settings_model,
            str(config.get("env_prefix", "")).upper(),
            str(config.get("env_nested_delimiter", "")),
        )
    )


def is_documented(env_name: str, text: str) -> bool:
    """Whole-name match: ``EVNT_COMMON__DEMO_DIR`` does not document ``EVNT_COMMON__DEMO``."""
    return re.search(rf"(?<![A-Z0-9_]){re.escape(env_name)}(?![A-Z0-9_])", text) is not None


def undocumented(env_vars: list[str], text: str, excluded: frozenset[str]) -> list[str]:
    return [name for name in env_vars if name not in excluded and not is_documented(name, text)]


def _docs_dir() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "website" / "docs"
        if candidate.is_dir() and (parent / "backend").is_dir():
            return candidate
    return None


@pytest.fixture(scope="module")
def docs_text() -> str:
    docs_dir = _docs_dir()
    if docs_dir is None:
        pytest.skip("website/docs is not available (installed package, not a checkout)")
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(docs_dir.rglob("*.md")))


def test_every_setting_is_documented(docs_text):
    missing = undocumented(settings_env_names(Settings), docs_text, UNDOCUMENTED)

    assert not missing, (
        "Settings missing from website/docs (add a row to configure/settings.md "
        f"or, if not user-facing, to UNDOCUMENTED): {missing}"
    )


def test_out_of_model_env_vars_are_documented(docs_text):
    assert undocumented(list(OUT_OF_MODEL_ENV_VARS), docs_text, frozenset()) == []


def test_exclusions_name_existing_settings():
    assert set(settings_env_names(Settings)) >= UNDOCUMENTED


def test_walk_reaches_nested_leaves_and_secrets():
    names = settings_env_names(Settings)

    assert "EVNT_INGEST__DIRECT__WAIT_FOR_ASYNC_INSERT" in names
    assert "EVNT_COMMON__SNOWPLOW__ENDPOINTS__POST_ENDPOINT" in names
    assert "EVNT_CLICKHOUSE__CONNECTION__PASSWORD" in names
    assert "EVNT_CLICKHOUSE__CONFIGURATION__TABLES" in names
    assert "EVNT_INGEST" not in names
    assert len(names) == len(set(names))


def test_a_new_undocumented_leaf_is_reported(docs_text):
    class Extra(BaseModel):
        brand_new_knob: int = 1

    class Extended(Settings):
        extra: Extra = Extra()
        top_level_knob: bool = False

    missing = undocumented(settings_env_names(Extended), docs_text, UNDOCUMENTED)

    assert missing == ["EVNT_EXTRA__BRAND_NEW_KNOB", "EVNT_TOP_LEVEL_KNOB"]


def test_an_optional_nested_model_is_walked(docs_text):
    class Sub(BaseModel):
        secret_knob: int = 1

    class Other(BaseModel):
        other_knob: int = 1

    class Extended(Settings):
        opt: Sub | None = None
        either: Sub | Other | None = None

    missing = undocumented(settings_env_names(Extended), docs_text, UNDOCUMENTED)

    assert missing == [
        "EVNT_OPT__SECRET_KNOB",
        "EVNT_EITHER__SECRET_KNOB",
        "EVNT_EITHER__OTHER_KNOB",
    ]


def test_a_longer_name_does_not_document_its_prefix():
    text = "`EVNT_COMMON__DEMO_DIR` and `XEVNT_COMMON__DEMO`"

    assert not is_documented("EVNT_COMMON__DEMO", text)
    assert is_documented("EVNT_COMMON__DEMO_DIR", text)
    assert is_documented("EVNT_COMMON__DEMO", "EVNT_COMMON__DEMO=true")
