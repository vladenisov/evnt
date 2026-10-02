import pathlib

from evnt.tracker import iglu as iglu_module


def _write_schema(
    base_dir: pathlib.Path,
    schema_uri: str,
    content: str,
) -> pathlib.Path:
    schema_path = iglu_module.resolve_iglu_schema_path(schema_uri)
    assert schema_path is not None

    relative_path = schema_path.relative_to(iglu_module.IGLU_SCHEMAS_DIR)
    target_path = base_dir / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")
    return target_path


def test_validate_iglu_payload_accepts_valid_payload(monkeypatch, tmp_path):
    schema_uri = "iglu:com.acme/example/jsonschema/1-0-0"
    monkeypatch.setattr(iglu_module, "IGLU_SCHEMAS_DIR", tmp_path)
    iglu_module.clear_iglu_caches()
    _write_schema(
        tmp_path,
        schema_uri,
        """
        {
          "type": "object",
          "properties": {
            "name": {"type": "string"}
          },
          "required": ["name"],
          "additionalProperties": false
        }
        """,
    )

    result = iglu_module.validate_iglu_payload(schema_uri, {"name": "ok"})

    assert result.status == "ok"
    assert (
        result.schema_path == tmp_path / "com.acme" / "example" / "jsonschema" / "1-0-0"
    )


def test_validate_iglu_payload_warns_when_schema_file_is_missing(monkeypatch, tmp_path):
    schema_uri = "iglu:com.acme/missing/jsonschema/1-0-0"
    monkeypatch.setattr(iglu_module, "IGLU_SCHEMAS_DIR", tmp_path)
    iglu_module.clear_iglu_caches()

    result = iglu_module.validate_iglu_payload(schema_uri, {"name": "ok"})

    assert result.status == "warning"
    assert result.error == "schema file not found"
    assert (
        result.schema_path == tmp_path / "com.acme" / "missing" / "jsonschema" / "1-0-0"
    )


def test_validate_iglu_payload_warns_when_schema_json_is_invalid(monkeypatch, tmp_path):
    schema_uri = "iglu:com.acme/bad_json/jsonschema/1-0-0"
    monkeypatch.setattr(iglu_module, "IGLU_SCHEMAS_DIR", tmp_path)
    iglu_module.clear_iglu_caches()
    _write_schema(tmp_path, schema_uri, "{not-json")

    result = iglu_module.validate_iglu_payload(schema_uri, {"name": "ok"})

    assert result.status == "warning"
    assert "schema JSON decode failed" in result.error


def test_validate_iglu_payload_warns_when_schema_is_invalid(monkeypatch, tmp_path):
    schema_uri = "iglu:com.acme/bad_schema/jsonschema/1-0-0"
    monkeypatch.setattr(iglu_module, "IGLU_SCHEMAS_DIR", tmp_path)
    iglu_module.clear_iglu_caches()
    _write_schema(
        tmp_path,
        schema_uri,
        """
        {
          "type": 1
        }
        """,
    )

    result = iglu_module.validate_iglu_payload(schema_uri, {"name": "ok"})

    assert result.status == "warning"
    assert "schema compilation failed" in result.error


def test_validate_iglu_payload_warns_when_payload_is_invalid(monkeypatch, tmp_path):
    schema_uri = "iglu:com.acme/example/jsonschema/1-0-0"
    monkeypatch.setattr(iglu_module, "IGLU_SCHEMAS_DIR", tmp_path)
    iglu_module.clear_iglu_caches()
    _write_schema(
        tmp_path,
        schema_uri,
        """
        {
          "type": "object",
          "properties": {
            "name": {"type": "string"}
          },
          "required": ["name"],
          "additionalProperties": false
        }
        """,
    )

    result = iglu_module.validate_iglu_payload(schema_uri, {"age": 10})

    assert result.status == "warning"
    assert "required property" in result.error


def test_validate_iglu_payload_skips_short_internal_uris(monkeypatch, tmp_path):
    monkeypatch.setattr(iglu_module, "IGLU_SCHEMAS_DIR", tmp_path)
    iglu_module.clear_iglu_caches()

    result = iglu_module.validate_iglu_payload(
        "iglu:dev.snowplow.simple/page_data",
        {"id": "123"},
    )

    assert result.status == "skipped"
    assert result.schema_path is None
    assert result.error is None


def test_warm_iglu_schema_cache_preloads_validator(monkeypatch, tmp_path):
    schema_uri = "iglu:com.acme/example/jsonschema/1-0-0"
    monkeypatch.setattr(iglu_module, "IGLU_SCHEMAS_DIR", tmp_path)
    iglu_module.clear_iglu_caches()
    _write_schema(
        tmp_path,
        schema_uri,
        """
        {
          "type": "object",
          "properties": {
            "name": {"type": "string"}
          },
          "required": ["name"],
          "additionalProperties": false
        }
        """,
    )

    results = iglu_module.warm_iglu_schema_cache([schema_uri])

    assert results[schema_uri].status == "ok"

    def _fail_json_load(*args, **kwargs):
        raise AssertionError("schema should already be cached")

    monkeypatch.setattr(iglu_module.json, "load", _fail_json_load)

    result = iglu_module.validate_iglu_payload(schema_uri, {"name": "ok"})

    assert result.status == "ok"
