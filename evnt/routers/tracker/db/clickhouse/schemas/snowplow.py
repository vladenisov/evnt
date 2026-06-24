"""
Snowplow schema field definitions for ClickHouse.
"""

from typing import NamedTuple

from clickhouse_connect.cc_sqlalchemy.datatypes.base import ChSqlaType
from clickhouse_connect.cc_sqlalchemy.datatypes.sqltypes import LowCardinality
from clickhouse_connect.datatypes.base import ClickHouseType, TypeDef
from clickhouse_connect.datatypes.container import Tuple
from clickhouse_connect.datatypes.dynamic import JSON as JSONType  # noqa: N811
from clickhouse_connect.datatypes.network import IPv4
from clickhouse_connect.datatypes.numeric import Bool, Enum8, Float32, UInt64
from clickhouse_connect.datatypes.special import UUID as UUIDType  # noqa: N811
from clickhouse_connect.datatypes.string import String
from clickhouse_connect.datatypes.temporal import DateTime64
from clickhouse_connect.driver.binding import quote_identifier


class ColumnDef(NamedTuple):
    payload_name: str | None
    name: str
    type: ClickHouseType | ChSqlaType | LowCardinality
    default_type: str | None = None
    default_expression: str | None = None
    comment: str | None = None
    codec_expression: str | None = None
    ttl_expression: str | None = None

    @property
    def type_name(self) -> str:
        return self.type.name

    @property
    def create_expression(self) -> str:
        type_name = self.type_name

        parts = [quote_identifier(self.name), type_name]
        if self.default_expression is not None:
            # Emit the DEFAULT/MATERIALIZED clause whenever an expression is
            # declared; ``default_type`` defaults to ``DEFAULT`` so a column that
            # only sets ``default_expression`` still gets its intended default.
            default_type = self.default_type or "DEFAULT"
            parts.append(f"{default_type} {self.default_expression}")
        if self.comment:
            parts.append(f"COMMENT {quote_identifier(self.comment)}")
        if self.codec_expression:
            parts.append(f"CODEC {self.codec_expression}")
        if self.ttl_expression:
            parts.append(f"TTL {self.ttl_expression}")

        return " ".join(parts)


class TupleColumnDef(NamedTuple):
    name: str
    elements: tuple[ColumnDef, ...]
    default_type: str | None = None
    default_expression: tuple[str, ...] | None = None
    comment: str | None = None
    codec_expression: str | None = None
    ttl_expression: str | None = None

    @property
    def type(self):
        return Tuple(
            type_def=TypeDef(
                keys=tuple(col.name for col in self.elements),
                values=tuple(col.type_name for col in self.elements),
            ),
        )

    @property
    def type_name(self) -> str:
        return self.type.name

    @property
    def create_expression(self) -> str:
        return f"{quote_identifier(self.name)} {self.type_name}"


STRING = String(type_def=TypeDef())
STRING_LC = LowCardinality(STRING)
UINT64 = UInt64(type_def=TypeDef())
FLOAT32 = Float32(type_def=TypeDef())
BOOL = Bool(type_def=TypeDef())
DATETIME64 = DateTime64(type_def=TypeDef(values=(3, "'UTC'")))
UUID = UUIDType(type_def=TypeDef())
IPV4 = IPv4(type_def=TypeDef())
JSON = JSONType(type_def=TypeDef())

PLATFORM = {
    "web": 1,
    "mob": 2,
    "pc": 3,
    "srv": 4,
    "app": 5,
    "tv": 6,
    "cnsl": 7,
    "iot": 8,
}
EVENT_TYPES = {
    "pv": 1,  # Page view
    "pp": 2,  # Page ping
    "ue": 3,  # Unstructured event
    "se": 4,  # Structured event
    "tr": 5,  # Transaction
    "ti": 6,  # Transaction item
    "s": 7,  # Session
}


# Field definitions for the Snowplow table
snowplow_fields: list[ColumnDef | TupleColumnDef] = [
    ColumnDef(
        payload_name="aid",
        name="app_id",
        type=STRING_LC,
    ),
    ColumnDef(
        payload_name="p",
        name="platform",
        type=Enum8(
            type_def=TypeDef(
                keys=tuple(PLATFORM.keys()),
                values=tuple(PLATFORM.values()),
            ),
        ),
    ),
    TupleColumnDef(
        name="app_info",
        elements=(
            ColumnDef(
                payload_name="app_version",
                name="version",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="app_build",
                name="build",
                type=STRING_LC,
            ),
        ),
    ),
    ColumnDef(
        payload_name="url",
        name="page",
        type=STRING,
        default_type="DEFAULT",
        default_expression="''",
    ),
    ColumnDef(
        payload_name="refr",
        name="referer",
        type=STRING,
        default_type="DEFAULT",
        default_expression="''",
    ),
    ColumnDef(
        payload_name="e",
        name="event_type",
        type=Enum8(
            type_def=TypeDef(
                keys=tuple(EVENT_TYPES.keys()),
                values=tuple(EVENT_TYPES.values()),
            ),
        ),
    ),
    ColumnDef(payload_name="eid", name="event_id", type=UUID),
    ColumnDef(payload_name="view_id", name="view_id", type=UUID),
    ColumnDef(payload_name="sid", name="session_id", type=UUID),
    ColumnDef(
        payload_name="vid",
        name="visit_count",
        type=UINT64,
        default_type="DEFAULT",
        default_expression="0",
    ),
    TupleColumnDef(
        name="session",
        elements=(
            ColumnDef(
                payload_name="event_index",
                name="event_index",
                type=UINT64,
                default_type="DEFAULT",
                default_expression="0",
            ),
            ColumnDef(
                payload_name="previous_session_id",
                name="previous_session_id",
                type=UUID,
            ),
            ColumnDef(
                payload_name="first_event_id",
                name="first_event_id",
                type=UUID,
            ),
            ColumnDef(
                payload_name="first_event_time",
                name="first_event_time",
                type=DATETIME64,
            ),
            ColumnDef(
                payload_name="storage_mechanism",
                name="storage_mechanism",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="session_unstructured",
                name="unstructured",
                type=JSON,
            ),
        ),
    ),
    ColumnDef(
        payload_name="amp",
        name="amp",
        type=JSON,
        default_expression="{}",
    ),
    ColumnDef(payload_name="duid", name="device_id", type=UUID),
    ColumnDef(
        payload_name="uid",
        name="user_id",
        type=STRING,
        default_type="DEFAULT",
        default_expression="''",
    ),
    ColumnDef(
        payload_name="dtm",
        name="time",
        type=DATETIME64,
    ),
    TupleColumnDef(
        name="time_extra",
        elements=(
            ColumnDef(
                payload_name="rtm",
                name="received",
                type=DATETIME64,
            ),
            ColumnDef(
                payload_name="stm",
                name="sent",
                type=DATETIME64,
            ),
        ),
    ),
    ColumnDef(
        payload_name="tz",
        name="timezone",
        type=STRING_LC,
        default_type="DEFAULT",
        default_expression="''",
    ),
    ColumnDef(
        payload_name="page",
        name="title",
        type=STRING,
        default_type="DEFAULT",
        default_expression="''",
    ),
    ColumnDef(
        payload_name="screen",
        name="screen",
        type=JSON,
    ),
    ColumnDef(
        payload_name="page_data",
        name="page_data",
        type=JSON,
    ),
    ColumnDef(
        payload_name="user_data",
        name="user_data",
        type=JSON,
        default_expression="{}",
    ),
    ColumnDef(payload_name="user_ip", name="user_ip", type=IPV4),
    ColumnDef(
        payload_name="geolocation",
        name="geolocation",
        type=JSON,
        default_expression="{}",
    ),
    ColumnDef(
        payload_name="user_agent",
        name="user_agent",
        type=STRING,
        default_type="DEFAULT",
        default_expression="''",
    ),
    TupleColumnDef(
        name="browser",
        elements=(
            ColumnDef(
                payload_name="browser_family",
                name="family",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="browser_version_string",
                name="version",
                type=STRING,
            ),
            ColumnDef(
                payload_name="browser_extra",
                name="extra",
                type=JSON,
            ),
        ),
    ),
    TupleColumnDef(
        name="os",
        elements=(
            ColumnDef(
                payload_name="os_family",
                name="family",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="os_version_string",
                name="version",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="lang",
                name="language",
                type=STRING_LC,
            ),
        ),
    ),
    TupleColumnDef(
        name="device",
        elements=(
            ColumnDef(
                payload_name="device_brand",
                name="brand",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="device_model",
                name="model",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="device_extra",
                name="extra",
                type=JSON,
            ),
        ),
    ),
    TupleColumnDef(
        name="device_is",
        elements=(
            ColumnDef(
                payload_name="device_is_mobile",
                name="mobile",
                type=BOOL,
            ),
            ColumnDef(
                payload_name="device_is_tablet",
                name="tablet",
                type=BOOL,
            ),
            ColumnDef(
                payload_name="device_is_touch_capable",
                name="touch",
                type=BOOL,
            ),
            ColumnDef(
                payload_name="device_is_pc",
                name="pc",
                type=BOOL,
            ),
            ColumnDef(
                payload_name="device_is_bot",
                name="bot",
                type=BOOL,
            ),
        ),
    ),
    TupleColumnDef(
        name="resolution",
        elements=(
            ColumnDef(payload_name="res", name="browser", type=STRING),
            ColumnDef(
                payload_name="vp",
                name="viewport",
                type=STRING,
            ),
            ColumnDef(
                payload_name="ds",
                name="page",
                type=STRING,
            ),
        ),
    ),
    TupleColumnDef(
        name="event",
        elements=(
            ColumnDef(
                payload_name="se_ac",
                name="action",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="se_ca",
                name="category",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="se_la",
                name="label",
                type=STRING,
            ),
            ColumnDef(
                payload_name="se_pr",
                name="property",
                type=JSON,
            ),
            ColumnDef(
                payload_name="se_va",
                name="value",
                type=FLOAT32,
            ),
            ColumnDef(
                payload_name="ue",
                name="unstructured",
                type=JSON,
            ),
        ),
    ),
    ColumnDef(
        payload_name="extra",
        name="extra",
        type=JSON,
        default_expression="{}",
    ),
    TupleColumnDef(
        name="tracker",
        elements=(
            ColumnDef(
                payload_name="tv",
                name="version",
                type=STRING_LC,
            ),
            ColumnDef(
                payload_name="tna",
                name="namespace",
                type=STRING_LC,
            ),
        ),
    ),
    ColumnDef(
        payload_name=None,
        name="app",
        type=STRING_LC,
        default_type="MATERIALIZED",
        default_expression="if(platform = 'mob', tracker.namespace, app_id)",
    ),
]
