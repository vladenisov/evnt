"""
IP address parsing and handling.
"""

from ipaddress import IPv4Address, IPv6Address, ip_address

from evnt.observability.tracing import capture_span

DEFAULT_IPV4 = IPv4Address("0.0.0.0")


@capture_span()
def extract_ip_from_header(
    header_value: str | None,
) -> IPv4Address | IPv6Address | None:
    """
    Parse a header value and return the first valid IP address.

    For comma-separated headers (for example, ``X-Forwarded-For``), the first
    valid value is treated as the original client IP.
    """
    if not header_value:
        return None

    for part in header_value.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            return ip_address(candidate)
        except ValueError:
            continue

    return None


@capture_span()
def convert_ip(ip: IPv4Address | IPv6Address | str | None) -> IPv4Address:
    """Convert an IP address to IPv4Address format."""

    if ip is None:
        return DEFAULT_IPV4
    if isinstance(ip, IPv4Address):
        return ip
    if isinstance(ip, str):
        ip = extract_ip_from_header(ip)
        if ip is None:
            return DEFAULT_IPV4

    if isinstance(ip, IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        return DEFAULT_IPV4

    return ip
