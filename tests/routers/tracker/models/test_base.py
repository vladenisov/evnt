"""Characterization tests for the ``Model.model_validate_json`` repair path.

``Model`` overrides ``model_validate_json`` so that malformed JSON is first
auto-repaired with ``json_repair`` before a second validation attempt. These
tests exercise both the happy path (valid JSON parsed directly) and the repair
fallback across ``str``/``bytes``/``bytearray``/``memoryview`` inputs, plus the
case where even repaired JSON still fails validation.
"""

import pytest
from pydantic import Field, ValidationError
from routers.tracker.models.base import Model


class _Person(Model):
    name: str = Field(...)
    age: int = Field(0)


def test_valid_json_string_parses_without_repair():
    person = _Person.model_validate_json('{"name": "Alice", "age": 30}')

    assert person.name == "Alice"
    assert person.age == 30


def test_malformed_json_string_is_repaired():
    # Trailing comma + single-quoted keys are invalid JSON; json_repair fixes them.
    person = _Person.model_validate_json("{'name': 'Bob', 'age': 41,}")

    assert person.name == "Bob"
    assert person.age == 41


def test_malformed_json_bytes_is_decoded_and_repaired():
    person = _Person.model_validate_json(b"{name: Carol, age: 5}")

    assert person.name == "Carol"
    assert person.age == 5


def test_malformed_json_bytearray_is_repaired():
    person = _Person.model_validate_json(bytearray(b"{name: Dave}"))

    assert person.name == "Dave"
    assert person.age == 0


def test_malformed_json_memoryview_is_repaired():
    person = _Person.model_validate_json(memoryview(b"{name: Eve, age: 9}"))

    assert person.name == "Eve"
    assert person.age == 9


def test_bytes_with_invalid_utf8_are_decoded_with_errors_ignored():
    # 0xff is not valid UTF-8; the repair path decodes with errors="ignore".
    person = _Person.model_validate_json(b'{"name": "F\xffrank"}')

    assert person.name.startswith("F")
    assert person.age == 0


def test_repair_that_still_fails_validation_raises_validation_error():
    # No way to coerce a complete missing required ``name`` field, so the
    # second validation attempt still raises ValidationError.
    with pytest.raises(ValidationError):
        _Person.model_validate_json("not json at all []")
