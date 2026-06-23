"""Tests for the ``/proxy/hash`` endpoint (``proxy_hash``).

``proxy_hash`` rewrites a third-party asset URL into a same-origin proxied URL
when the URL's host and path match the configured proxy allowlists, and returns
the URL unchanged otherwise. The function is called directly here (mirroring the
direct-call style used in ``tests/test_proxy.py``); it only depends on the
module-level proxy configuration constants, so no app/network is needed.
"""

import base64

import pytest

from evnt.routers import proxy as proxy_module
from evnt.routers.proxy import models


def _decode(value: str) -> str:
    return base64.urlsafe_b64decode(value).decode("utf-8")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_proxy_hash_rewrites_matching_url(anyio_backend):
    # Host and path both match the default proxy allowlists, so the URL must be
    # rewritten to point at the configured app hostname.
    data = models.HashModel(url="https://google-analytics.com/analytics.js")

    result = await proxy_module.proxy_hash(data)
    result_str = str(result)

    hostname = proxy_module.HOSTNAME
    # The rewritten URL points back at the app host (http://localhost:8000).
    assert result_str.startswith(f"{hostname.scheme}://{hostname.host}:{hostname.port}")
    # It is routed through the proxy endpoint's /route/<scheme>/ path.
    assert f"{proxy_module.PROXY_ENDPOINT}/route/https/" in result_str

    # The encoded host and path round-trip back to the originals.
    route_marker = f"{proxy_module.PROXY_ENDPOINT}/route/https/".lstrip("/")
    encoded_tail = result_str.split(route_marker, 1)[1]
    encoded_host, encoded_path = encoded_tail.split("/", 1)
    assert _decode(encoded_host) == "google-analytics.com"
    assert _decode(encoded_path) == "analytics.js"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_proxy_hash_returns_original_for_non_matching_url(anyio_backend):
    # Neither the host nor the path is on the allowlist, so the URL is returned
    # unchanged (no proxying).
    original = "https://example.com/some/other/script.js"
    data = models.HashModel(url=original)

    result = await proxy_module.proxy_hash(data)

    assert str(result) == original
    # It must not be rewritten through the proxy endpoint.
    assert f"{proxy_module.PROXY_ENDPOINT}/route/" not in str(result)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_proxy_hash_rewrites_when_only_host_matches(anyio_backend):
    # A matching host alone (with a non-allowlisted path) is still proxied,
    # because matching the domain flips the encode flag.
    data = models.HashModel(url="https://google-analytics.com/g/collect?v=2")

    result = await proxy_module.proxy_hash(data)

    assert f"{proxy_module.PROXY_ENDPOINT}/route/https/" in str(result)
    hostname = proxy_module.HOSTNAME
    assert str(result).startswith(
        f"{hostname.scheme}://{hostname.host}:{hostname.port}",
    )
