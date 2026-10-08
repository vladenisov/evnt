from evnt.tracker import ip as ip_module

convert_ip = ip_module.convert_ip
extract_ip_from_header = ip_module.extract_ip_from_header
DEFAULT_IPV4 = ip_module.DEFAULT_IPV4


def test_extract_ip_from_header_uses_first_ip_from_forward_chain():
    result = extract_ip_from_header(
        "203.0.113.1,198.51.100.101,198.51.100.102",
    )

    assert str(result) == "203.0.113.1"


def test_extract_ip_from_header_skips_invalid_values():
    result = extract_ip_from_header("unknown, not-an-ip, 198.51.100.44")

    assert str(result) == "198.51.100.44"


def test_convert_ip_from_forward_chain():
    result = convert_ip("203.0.113.1,198.51.100.101")

    assert str(result) == "203.0.113.1"


def test_convert_ip_invalid_chain_returns_default_ipv4():
    result = convert_ip("unknown,not-an-ip")

    assert result == DEFAULT_IPV4
