"""Building the stored row of one event from its validated payload element.

Everything here works on values the element model has already validated, so
none of it needs the malformed-input logging of ``evnt.tracker.payload``.
"""

from copy import copy
from datetime import UTC
from ipaddress import IPv4Address
from typing import Any

import orjson

from evnt.tracker.element import EventIssues
from evnt.tracker.models import InsertModel, PayloadElementModel, UserAgentModel

_SE_PR_JSON_ERRORS = (orjson.JSONDecodeError, TypeError)

# The ``platform`` column is an Enum8 without these codes, so they are stored
# as the closest code it has; the code as sent goes to ``extra.tracker.platform``.
# ``headset`` (visionOS and other headsets, sent by the iOS and Android trackers)
# groups with ``mob``: the events carry the mobile contexts and screen views.
STORED_PLATFORM: dict[str, str] = {"headset": "mob"}

_MUTABLE_USER_AGENT_FIELDS = (
    "browser_version",
    "browser_extra",
    "os_version",
    "device_extra",
)

# Resolve static InsertModel defaults once. Passing the complete set of static
# fields to model_construct avoids Pydantic deep-copying every mutable default
# for every event. Fields backed by a default_factory stay unresolved here so a
# partially constructed PayloadElementModel still gets fresh UUID/timestamps.
_INSERT_MODEL_DEFAULTS = {
    field_name: value
    # Constructed bare on purpose: only the defaults are wanted here.
    for field_name, value in InsertModel.model_construct().__dict__.items()  # type: ignore[call-arg]
    if InsertModel.model_fields[field_name].default_factory is None
}
_MUTABLE_INSERT_DEFAULT_FIELDS = tuple(
    field_name
    for field_name, value in _INSERT_MODEL_DEFAULTS.items()
    if isinstance(value, (dict, list, set))
)


def build_initial_model(
    element: PayloadElementModel,
    user_agent: UserAgentModel | dict[str, Any],
    ip: IPv4Address,
) -> InsertModel:
    """Merge payload + UA + IP into a fresh InsertModel.

    ``user_agent`` is a model or just its field values as a dict (what the
    ingest path passes, to skip building a model per request).
    """

    # model_construct skips validation, so document the invariant the caller
    # must uphold: ``ip`` is an already-validated IPv4Address.
    assert isinstance(ip, IPv4Address)

    data = _INSERT_MODEL_DEFAULTS.copy()
    for field_name in _MUTABLE_INSERT_DEFAULT_FIELDS:
        data[field_name] = copy(data[field_name])

    data.update(user_agent if type(user_agent) is dict else user_agent.__dict__)
    for field_name in _MUTABLE_USER_AGENT_FIELDS:
        data[field_name] = copy(data[field_name])
    data.update(element.__dict__)
    data["user_ip"] = ip
    return InsertModel.model_construct(**data)


def dump_insert_model(model: InsertModel) -> dict[str, Any]:
    """Return a ClickHouse row dict without running Pydantic serialization."""

    return model.__dict__.copy()


def apply_platform(result: InsertModel, issues: EventIssues) -> None:
    """Store a platform code the Enum8 column lacks as its closest code."""

    stored = STORED_PLATFORM.get(result.p)
    if stored is not None:
        issues.track("platform", result.p)
        result.p = stored  # type: ignore[assignment]


def apply_true_timestamp(
    element: PayloadElementModel, result: InsertModel, issues: EventIssues
) -> None:
    """Keep ``ttm`` and use it as the event time when ``dtm`` is missing.

    The JS tracker sends ``ttm`` instead of ``dtm`` when the caller supplies
    the true timestamp, so without this the server time would be stored.
    """

    ttm = element.ttm
    if ttm is None:
        return
    if "dtm" not in element.model_fields_set:
        result.dtm = ttm
    utc = ttm.replace(tzinfo=UTC) if ttm.tzinfo is None else ttm.astimezone(UTC)
    issues.track("true_tstamp", utc.isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def keep_replaced_urls(
    element: PayloadElementModel, result: InsertModel, issues: EventIssues
) -> None:
    """Keep the tracker's own ``url``/``refr`` when parsing replaced them.

    The mobile trackers copy a deep link into ``url``/``refr`` for campaign
    attribution, and a screen view then stores the screen names there.
    """

    if element.url and result.url != element.url:
        issues.track("url", element.url)
    if element.refr and result.refr != element.refr:
        issues.track("refr", element.refr)


def normalize_se_property(result: InsertModel) -> None:
    """Force ``result.se_pr`` to a dict, wrapping non-JSON strings."""

    if not (result.se_pr and isinstance(result.se_pr, str)):
        result.se_pr = {}
        return

    try:
        result.se_pr = orjson.loads(result.se_pr)
    except _SE_PR_JSON_ERRORS:
        result.se_pr = {"ex-property": result.se_pr}

    if not isinstance(result.se_pr, dict):
        result.se_pr = {"ex-property": result.se_pr}


def normalize_se_value(result: InsertModel) -> None:
    """Force ``result.se_va`` to a float, stashing non-numeric originals.

    Assumes ``normalize_se_property`` ran first so ``result.se_pr`` is a dict.
    """

    if not result.se_va:
        result.se_va = 0.0
        return

    if isinstance(result.se_va, float):
        return
    if isinstance(result.se_va, int):
        try:
            # Only checks the range; a valid integer is stored as it came.
            float(result.se_va)
        except OverflowError:
            # Kept as text: the JSON column cannot hold an integer this large.
            if isinstance(result.se_pr, dict):
                result.se_pr["ex-value"] = str(result.se_va)
            result.se_va = 0.0
        return

    try:
        result.se_va = float(result.se_va)
    except ValueError:
        if isinstance(result.se_pr, dict):
            result.se_pr["ex-value"] = result.se_va
        result.se_va = 0.0


def build_fallback_model(
    element: PayloadElementModel,
    user_agent: UserAgentModel | dict[str, Any],
    ip: IPv4Address,
    issues: EventIssues,
) -> InsertModel:
    """Build the row of an event whose full parsing raised.

    Only the validated atomic fields go in: contexts and the self-describing
    event are left out, since whatever state parsing reached is suspect.
    """

    result = build_initial_model(element, user_agent, ip)
    apply_platform(result, issues)
    apply_true_timestamp(element, result, issues)
    normalize_se_property(result)
    normalize_se_value(result)
    return result
