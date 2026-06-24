"""Behavioral tests for the SendGrid event model validators.

Covers the lenient-but-strict ``email`` field validator (strip, non-empty,
must contain ``@``, must be a string) and the alias mapping for ``smtp-id``.
"""

import pytest
from pydantic import ValidationError
from routers.tracker.models.sendgrid import SendgridElementBaseModel


def _valid_event(**overrides):
    base = {
        "email": "user@example.com",
        "timestamp": 1700000000,
        "smtp-id": "<abc@sendgrid>",
        "event": "delivered",
        "category": ["welcome"],
        "sg_event_id": "evt-1",
        "sg_message_id": "msg-1",
    }
    base.update(overrides)
    return base


def test_valid_event_parses_and_maps_smtp_id_alias():
    event = SendgridElementBaseModel.model_validate(_valid_event())

    assert event.email == "user@example.com"
    assert event.smtp_id == "<abc@sendgrid>"
    assert event.event == "delivered"
    assert event.category == ["welcome"]


def test_email_is_stripped_of_surrounding_whitespace():
    event = SendgridElementBaseModel.model_validate(
        _valid_event(email="  spaced@example.com  "),
    )

    assert event.email == "spaced@example.com"


def test_empty_email_is_rejected():
    with pytest.raises(ValidationError, match="email must not be empty"):
        SendgridElementBaseModel.model_validate(_valid_event(email="   "))


def test_email_without_at_sign_is_rejected():
    with pytest.raises(ValidationError, match="email must contain"):
        SendgridElementBaseModel.model_validate(_valid_event(email="not-an-email"))


def test_non_string_email_is_rejected():
    with pytest.raises(ValidationError, match="email must be a string"):
        SendgridElementBaseModel.model_validate(_valid_event(email=12345))
