"""
Core data processing handlers for Snowplow events.
"""

from ipaddress import IPv4Address, IPv6Address
from typing import Any

import structlog

from evnt.observability.tracing import async_capture_span
from evnt.tracker.ip import convert_ip
from evnt.tracker.models import (
    PayloadElementModel,
    PayloadModel,
)
from evnt.tracker.payload import dump_insert_model, parse_payload
from evnt.tracker.useragent import parse_agent_for_insert_async

logger = structlog.get_logger(__name__)


@async_capture_span()
async def process_data(
    body: PayloadElementModel | PayloadModel,
    user_agent: str | None,
    user_ip: IPv4Address | IPv6Address | str | None,
    cookies: str | None,
) -> list[dict[str, Any]]:
    """
    Process incoming event data from various sources.

    This function:
    1. Processes the IP address
    2. Parses the user agent
    3. Extracts and processes payload data
    4. Combines all information into complete event records

    Args:
        body: The request body or parameters
        user_agent: User agent string from headers
        user_ip: IP address from headers
        cookies: Cookie string from headers

    Returns:
        List of processed event records ready for storage
    """
    user_ip = convert_ip(user_ip)
    ua_data = await parse_agent_for_insert_async(user_agent)

    # Extract payload data
    data = body.data if isinstance(body, PayloadModel) else [body]

    # Process each payload element
    result = []
    for item in data:
        payload_data = await parse_payload(item, ua_data, user_ip, cookies)

        item_data = dump_insert_model(payload_data)
        result.append(item_data)
    return result
