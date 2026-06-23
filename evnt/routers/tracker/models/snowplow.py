"""
Data models for Snowplow events.
"""

import urllib.parse as urlparse
from datetime import UTC, datetime
from functools import partial
from ipaddress import IPv4Address
from typing import Any, Literal
from uuid import UUID, uuid4

from core.config import settings
from pydantic import (
    AliasChoices,
    Field,
    computed_field,
    field_validator,
)
from routers.tracker.parsers.utils import find_available

from .base import Model

schemas = settings.common.snowplow.schemas


DEFAULT_UUID = UUID("00000000-0000-0000-0000-000000000000")
DEFAULT_DATE = datetime(1970, 1, 1, tzinfo=UTC)

_utcnow = partial(datetime.now, UTC)


class SnowPlowModel(Model):
    """Base model for Snowplow data."""

    # ``data`` is intentionally ``list[Any]`` only on this abstract base: the
    # base is never instantiated/validated directly. Concrete subclasses tighten
    # the element type to restore schema enforcement on the real POST body wrapper
    # (e.g. ``PayloadModel.data: list[PayloadElementModel]`` below).
    data: list[Any] = Field(...)
    json_schema: str | None = Field(
        None,
        alias="schema",
        title="Snowplow schema definition",
    )


class StructuredEvent(Model):
    """Model for structured events."""

    se_ac: str = Field(
        "",
        title="Event action",
        description="Only for event_type = se",
        validation_alias=AliasChoices("se_ac", "action"),
    )
    se_ca: str = Field(
        "",
        title="Event category",
        description="Only for event_type = se",
        validation_alias=AliasChoices("se_ca", "category"),
    )
    se_la: str = Field(
        "",
        title="Event label",
        description="Only for event_type = se",
        validation_alias=AliasChoices("se_la", "label"),
    )
    se_pr: str | dict = Field(
        "",
        title="Event property",
        description="Only for event_type = se",
        validation_alias=AliasChoices("se_pr", "property"),
    )
    se_va: str | int | float = Field(
        "",
        title="Event value",
        description="Only for event_type = se",
        validation_alias=AliasChoices("se_va", "value"),
    )


class Validation(Model):
    aid: str = Field(..., title="Unique identifier for website / application")
    # URL and referrer fields
    url: str = Field("", title="Page URL")
    refr: str = Field("", title="Referrer URL")

    @field_validator("aid", mode="before")
    @classmethod
    def rename_aid(cls, v):
        if v == "undefined":
            return "other"
        return v

    @field_validator("refr", "url", mode="before")
    @classmethod
    def decode_url_fields(cls, v):
        return urlparse.unquote(v) if v else v


class Base(Model):
    e: Literal["pv", "pp", "ue", "se", "tr", "ti", "s"] = Field(
        ...,
        title="Event type",
        description="pv = page view, pp = page ping, ue = unstructured event, "
        "se = structured event, tr = transaction, ti = transaction item, s = session",
    )


class Contexts(Base):
    # Context information
    co: str | None = Field(None, title="An array of custom contexts")
    cx: str | None = Field(None, title="An array of custom contexts (b64)")

    # Unstructured event information
    ue_pr: str = Field("", title="The properties of the event")
    ue_px: str = Field("", title="The properties of the event (b64)")

    # Page ping information (for pp event type)
    pp_mix: int = Field(0, title="Minimum page x offset seen in the last ping period")
    pp_max: int = Field(0, title="Maximum page x offset seen in the last ping period")
    pp_miy: int = Field(0, title="Minimum page y offset seen in the last ping period")
    pp_may: int = Field(0, title="Maximum page y offset seen in the last ping period")

    @computed_field
    @property
    def ue_context(self) -> dict[str, Any] | None:
        return find_available(self.ue_pr, self.ue_px)

    @computed_field
    @property
    def contexts(self) -> dict[str, Any] | None:
        contexts = find_available(self.co, self.cx)
        return contexts

    @computed_field
    @property
    def ping_context(self) -> dict[str, Any] | None:
        if self.e != "pp":
            return None

        if self.pp_mix or self.pp_max or self.pp_miy or self.pp_may:
            return {
                "min_x": self.pp_mix,
                "max_x": self.pp_max,
                "min_y": self.pp_miy,
                "max_y": self.pp_may,
            }

        return None


class PayloadBase(Base, Validation, StructuredEvent):
    page: str = Field("", title="Page title")

    # Identifiers
    eid: UUID = Field(default_factory=uuid4, title="Event UUID")
    duid: UUID | None = Field(
        None,
        title="Unique identifier for a user, based on a first party cookie",
    )
    uid: str = Field(
        "",
        title="Unique identifier for user, set by the business using setUserId",
    )
    sid: UUID | None = Field(
        None,
        title="Unique identifier (UUID) for this visit of this user_id to this domain",
    )
    vid: int = Field(
        0,
        title="Index of number of visits that this user_id has made to this domain",
    )

    # Event timestamps
    dtm: datetime = Field(
        default_factory=_utcnow,
        title="Timestamp when event occurred, as recorded by client device",
    )
    stm: datetime = Field(
        default_factory=_utcnow,
        title="Timestamp when event was sent by client device to collector",
    )
    rtm: datetime = Field(
        default_factory=_utcnow,
        title="Timestamp when event was received by collector",
    )

    # Platform and environment
    p: Literal["web", "mob", "pc", "srv", "app", "tv", "cnsl", "iot"] = Field(
        ...,
        title="The platform the app runs on",
        description="i.e. web",
    )
    tv: str = Field(..., title="Identifier for Snowplow tracker")
    tna: str = Field("", title="The tracker namespace")
    tz: str = Field("", title="Time zone of client device's OS")
    lang: str = Field("", title="Language the browser is set to")

    # Browser/viewport information
    cs: str = Field("", title="Web page's character encoding", description="i.e. UTF-8")
    res: str = Field(..., title="Screen / monitor resolution")
    vp: str = Field("0x0", title="Browser viewport width and height")
    ds: str = Field("0x0", title="Web page width and height")
    cd: int = Field(0, title="Browser color depth")
    cookie: int | None = Field(None, title="Does the browser permit cookies?")


class PayloadElementModel(PayloadBase, Contexts):
    """Base model for payload elements (common to GET and POST)."""

    pass


class PayloadModel(SnowPlowModel):
    """Model for JSON payload in POST requests."""

    data: list[PayloadElementModel] = Field([])


class UserAgentModel(Model):
    user_agent: str = Field("", title="User agent string")
    browser_family: str = Field("", title="Browser family")
    browser_version: list[str] = Field([], title="Browser version")
    browser_version_string: str = Field("", title="Browser version string")
    browser_extra: dict[str, Any] = Field({}, title="Browser extra data")
    os_family: str = Field("", title="Operating system family")
    os_version: list[str] = Field([], title="Operating system version")
    os_version_string: str = Field("", title="Operating system version string")
    lang: str = Field("", title="Language")
    device_brand: str = Field("", title="Device brand")
    device_model: str = Field("", title="Device model")
    device_extra: dict[str, Any] = Field({}, title="Device extra data")
    device_is_mobile: bool = Field(False, title="Is device mobile?")
    device_is_tablet: bool = Field(False, title="Is device tablet?")
    device_is_touch_capable: bool = Field(False, title="Is device touch capable?")
    device_is_pc: bool = Field(False, title="Is device PC?")
    device_is_bot: bool = Field(False, title="Is device bot?")


class InsertModel(PayloadBase, UserAgentModel):
    ue: dict[str, Any] = Field({}, title="The unstructured event")
    extra: dict[str, Any] = Field(
        {},
        title="Extra fields not part of the standard payload",
    )
    user_data: dict[str, Any] = Field({}, title="User data")
    page_data: dict[str, Any] = Field({}, title="Page data")
    screen: dict[str, Any] = Field({}, title="Screen data")
    session_unstructured: dict[str, Any] = Field({}, title="Session unstructured data")
    browser_extra: dict[str, Any] = Field({}, title="Browser extra data")
    amp: dict[str, Any] = Field({}, title="AMP data")
    device_extra: dict[str, Any] = Field({}, title="Device extra data")
    geolocation: dict[str, Any] = Field({}, title="Geolocation data")

    app_version: str = Field("", title="App version")
    app_build: str = Field("", title="App build")
    storage_mechanism: str = Field("", title="Storage mechanism")

    first_event_time: datetime = Field(DEFAULT_DATE, title="First event time")
    view_id: UUID = Field(DEFAULT_UUID, title="View ID")
    previous_session_id: UUID = Field(DEFAULT_UUID, title="Previous session ID")
    first_event_id: UUID = Field(DEFAULT_UUID, title="First event ID")
    event_index: int = Field(0, title="Event index")

    user_ip: IPv4Address = Field(..., title="User IP address")
