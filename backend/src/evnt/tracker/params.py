"""Per-event overrides of request metadata: the ``ua``, ``ip`` and ``tnuid`` params.

Server-side trackers and WebView bridges send events on behalf of a client the
HTTP request does not come from, so the tracker can name that client's user
agent (``ua``) and IP address (``ip``). Every value sent is kept as sent in
``extra.tracker`` (``ua``, ``ip``, ``network_userid``), so an override is
always visible next to what it replaced.

``extra.collector.client_ip`` stays the connection's address in every case:
it records what the collector saw, while ``user_ip`` holds the address the
event is attributed to. A request whose ``SP-Anonymous`` header is honoured
stores neither, so its ``ip`` param is ignored and not kept.
"""

from ipaddress import IPv4Address, ip_address
from typing import Any, Final

from evnt.tracker.element import EventIssues, quote_value
from evnt.tracker.ip import convert_ip
from evnt.tracker.models import PayloadElementModel
from evnt.tracker.useragent import agent_fields_for_insert_async, cached_agent_fields

# Only server-side events: a browser or app could otherwise set any address
# it likes as its own, while a server-side tracker is configured by the site.
IP_OVERRIDE_PLATFORM = "srv"

# Distinct ``ua`` values one request may have parsed. A parser miss costs about
# 1 ms of CPU, so without a bound one 10 MiB batch with a different ``ua`` on
# every event costs tens of seconds and flushes the process-wide UA cache that
# header parsing relies on. Server-side trackers and WebView bridges send a
# handful of agents per batch; 32 keeps the worst request near 32 ms of parsing.
MAX_UA_OVERRIDES_PER_REQUEST: Final[int] = 32


class RequestAgents:
    """The parsed user agents of one request, the header's and the overrides'.

    A request carries one header but each event may name its own agent, so an
    override is looked up once per distinct value per request, through the
    process-wide parse cache, and at most ``MAX_UA_OVERRIDES_PER_REQUEST``
    distinct values are parsed per request.
    """

    __slots__ = ("_header_fields", "_overrides")

    def __init__(self, header_fields: dict[str, Any]) -> None:
        self._header_fields = header_fields
        self._overrides: dict[str, dict[str, Any]] = {}

    async def fields_for(self, element: PayloadElementModel, issues: EventIssues) -> dict[str, Any]:
        """The agent fields to store for ``element``: its ``ua``, else the header's.

        The result is shared by the events that use it; the row builder copies
        what it mutates.
        """

        ua = element.ua
        if not ua:
            return self._header_fields
        issues.track("ua", ua)
        fields = self._overrides.get(ua)
        if fields is not None:
            return fields
        if len(self._overrides) < MAX_UA_OVERRIDES_PER_REQUEST:
            fields = self._overrides[ua] = await agent_fields_for_insert_async(ua)
            return fields
        # Past the bound only a value the cache already knows is used: it costs
        # no parse and evicts nothing. A relay with many users still gets most
        # agents right, as earlier requests have parsed them.
        fields = cached_agent_fields(ua)
        if fields is None:
            issues.add(
                "payload",
                f"ua override ignored: more than {MAX_UA_OVERRIDES_PER_REQUEST} "
                "distinct ua values in one request",
                field="ua",
            )
            return self._header_fields
        return fields


def event_ip(
    element: PayloadElementModel,
    issues: EventIssues,
    connection_ip: IPv4Address,
    *,
    anonymous: bool = False,
) -> IPv4Address:
    """The ``user_ip`` of ``element``: its ``ip`` param for ``srv``, else the connection's.

    Also keeps the sent ``ip`` and ``tnuid`` in ``extra.tracker``. For an
    anonymous request (``SP-Anonymous`` honoured) the ``ip`` param is neither
    used nor kept: the client asked for no IP address to be stored.
    """

    if element.tnuid:
        issues.track("network_userid", element.tnuid)
    raw = element.ip
    if not raw or anonymous:
        return connection_ip
    issues.track("ip", raw)
    if element.p != IP_OVERRIDE_PLATFORM:
        return connection_ip
    try:
        address = ip_address(raw.strip())
    except ValueError:
        issues.add("payload", f"not an IP address, got {quote_value(raw)}", field="ip")
        return connection_ip
    # Same IPv4 mapping as the connection address: IPv6 is stored as 0.0.0.0
    # unless it is IPv4-mapped.
    return convert_ip(address)
