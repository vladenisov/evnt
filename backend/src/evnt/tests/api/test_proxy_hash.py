"""Tests for the ``/proxy/hash`` endpoint (``proxy_hash``).

``proxy_hash`` rewrites a third-party asset URL into a same-origin proxied URL
when the URL's host and path match the configured proxy allowlists, and returns
the URL unchanged otherwise. The function is called directly here (mirroring the
direct-call style used in ``tests/test_proxy.py``); it only depends on the
module-level proxy configuration constants, so no app/network is needed.
"""

import base64

import pytest

from evnt.api import proxy as proxy_module
from evnt.api.proxy import HashModel


def _decode(value: str) -> str:
    return base64.urlsafe_b64decode(value).decode("utf-8")


@pytest.mark.anyio
async def test_proxy_hash_rewrites_matching_url(anyio_backend):
    # Host and path both match the default proxy allowlists, so the URL must be
    # rewritten to point at the configured app hostname.
    data = HashModel(url="https://google-analytics.com/analytics.js")

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
async def test_proxy_hash_returns_original_for_non_matching_url(anyio_backend):
    # Neither the host nor the path is on the allowlist, so the URL is returned
    # unchanged (no proxying).
    original = "https://example.com/some/other/script.js"
    data = HashModel(url=original)

    result = await proxy_module.proxy_hash(data)

    assert str(result) == original
    # It must not be rewritten through the proxy endpoint.
    assert f"{proxy_module.PROXY_ENDPOINT}/route/" not in str(result)


@pytest.mark.anyio
async def test_proxy_hash_encodes_any_path_on_an_allowlisted_host():
    # GA4 and gtag URLs are multi-segment paths with a query string; both must
    # land in one base64 segment or the route cannot match them.
    data = HashModel(url="https://www.googletagmanager.com/gtag/js?id=G-TEST")

    result = str(await proxy_module.proxy_hash(data))

    route_marker = f"{proxy_module.PROXY_ENDPOINT}/route/https/".lstrip("/")
    encoded_host, encoded_path = result.split(route_marker, 1)[1].split("/")
    assert _decode(encoded_host) == "www.googletagmanager.com"
    assert _decode(encoded_path) == "gtag/js?id=G-TEST"


@pytest.mark.anyio
async def test_proxy_hash_leaves_urls_on_other_hosts_alone():
    # The route refuses hosts outside proxy.domains, so rewriting one would
    # only hand the page a URL that always answers 403.
    original = "https://cdn.example.com/analytics.js"

    result = await proxy_module.proxy_hash(HashModel(url=original))

    assert str(result) == original


def test_hashed_url_round_trips_through_the_route(monkeypatch):
    from types import SimpleNamespace
    from urllib.parse import urlsplit

    from fastapi.testclient import TestClient

    from evnt.tests.support import RecordingConnector, build_app

    requested = []

    class _Upstream:
        status_code = 200
        headers = {"Content-Type": "application/javascript"}

        async def aiter_bytes(self):
            yield b"// gtag"

        async def aclose(self):
            return None

    class _Client:
        def build_request(self, method, url):
            return SimpleNamespace(method=method, url=url)

        async def send(self, request, stream):
            requested.append(str(request.url))
            return _Upstream()

    app = build_app(monkeypatch)
    app.state.connector = RecordingConnector()
    app.state.proxy_http_client = _Client()
    app.state.proxy_allowed_hosts = frozenset({"www.googletagmanager.com"})
    app.state.proxy_allowed_ports = frozenset({443})
    client = TestClient(app)

    hashed = client.post(
        f"{proxy_module.PROXY_ENDPOINT}/hash",
        json={"url": "https://www.googletagmanager.com/gtag/js?id=G-TEST"},
    )
    response = client.get(urlsplit(hashed.json()).path)

    assert response.status_code == 200
    assert response.text == "// gtag"
    assert requested == ["https://www.googletagmanager.com/gtag/js?id=G-TEST"]
