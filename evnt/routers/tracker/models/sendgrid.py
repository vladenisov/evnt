"""
Data models for Sendgrid events.
"""

from datetime import datetime

from pydantic import Field, field_validator

from .base import Model


class SendgridElementBaseModel(Model):
    """Model for Sendgrid webhook events."""

    email: str = Field(..., title="Email address")

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Strip whitespace and reject empty / obviously-invalid addresses.

        Deliberately lenient (no full RFC validation, no extra dependency such
        as ``EmailStr``) so genuine SendGrid webhook payloads keep parsing; we
        only require a non-empty value containing an ``@`` separator.
        """
        if not isinstance(v, str):
            raise ValueError("email must be a string")
        email = v.strip()
        if not email:
            raise ValueError("email must not be empty")
        if "@" not in email:
            raise ValueError("email must contain '@'")
        return email

    timestamp: datetime = Field(..., title="Event timestamp")
    smtp_id: str = Field(..., validation_alias="smtp-id", title="SMTP ID")
    event: str = Field(..., title="Event type")
    category: list[str] = Field(..., title="Event categories")
    sg_event_id: str = Field(..., title="SendGrid event ID")
    sg_message_id: str = Field(..., title="SendGrid message ID")
    response: str = Field("", title="Response")
    attempt: int = Field(0, title="Attempt number")
    useragent: str = Field("", title="User agent string")
    ip: str = Field("", title="IP address")
    url: str = Field("", title="URL")
    reason: str = Field("", title="Reason")
    status: str = Field("", title="Status")
    asm_group_id: int = Field(0, title="ASM Group ID")


class SendgridModel(Model):
    """Model for batch Sendgrid events."""

    data: list[SendgridElementBaseModel] = Field([])
