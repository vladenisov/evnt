"""
Core data processing handlers for Snowplow events.
"""

import asyncio
from ipaddress import IPv4Address, IPv6Address
from typing import Any

import structlog

from evnt.observability.tracing import async_capture_span
from evnt.tracker.ip import convert_ip, extract_ip_from_header
from evnt.tracker.models import (
    PayloadElementModel,
    PayloadModel,
)
from evnt.tracker.payload import dump_insert_model, parse_payload
from evnt.tracker.useragent import agent_fields_for_insert_async

logger = structlog.get_logger(__name__)

# Events processed between yields to the event loop (about 10-20 ms of work).
_EVENTS_PER_YIELD = 64


@async_capture_span()
async def process_data(
    body: PayloadElementModel | PayloadModel,
    user_agent: str | None,
    user_ip: IPv4Address | IPv6Address | str | None,
    cookies: str | None,
    user_country: str | None = None,
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
        user_country: Validated country code from the trusted proxy header,
            stored as ``extra.collector.ip_country``

    Returns:
        List of processed event records ready for storage
    """
    client_ip = extract_ip_from_header(user_ip) if isinstance(user_ip, str) else user_ip
    user_ip = convert_ip(client_ip)
    ua_data = await agent_fields_for_insert_async(user_agent)

    # Extract payload data
    data = body.data if isinstance(body, PayloadModel) else [body]

    # Process each payload element
    result = []
    for index, item in enumerate(data):
        # parse_payload never really suspends, so without this a large batch
        # holds the event loop for its whole run (about 1 s for 5000 events)
        # and every other request waits behind it.
        if index and index % _EVENTS_PER_YIELD == 0:
            await asyncio.sleep(0)
        payload_data = await parse_payload(item, ua_data, user_ip, cookies)

        item_data = dump_insert_model(payload_data)
        item_data["extra"] = {
            **item_data["extra"],
            "collector": {
                "client_ip": str(client_ip) if client_ip is not None else "",
                "ip_country": user_country or "",
            },
        }
        result.append(item_data)
    return result
