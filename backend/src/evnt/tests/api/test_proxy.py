import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import Request
from structlog.testing import capture_logs

from evnt.api import proxy as proxy_module
from evnt.config import Settings
from evnt.tests.support import RecordingConnector, build_app


class _DummyResponse:
    status_code = 200
    headers = {"Content-Type": "text/plain"}
    closed = False

    async def aiter_bytes(self):
        yield b"ok"

    async def aclose(self):
        self.closed = True


class _DummyProxyClient:
    def __init__(
        self,
        event: asyncio.Event,
        requested_urls: list[str] | None = None,
    ):
        self._event = event
        self._requested_urls = requested_urls if requested_urls is not None else []
        self.responses: list[_DummyResponse] = []

    def build_request(self, method: str, url, headers=None):
        return SimpleNamespace(method=method, url=url)

    async def send(self, request, stream: bool):
        assert request.method == "GET"
        assert stream is True
        self._requested_urls.append(str(request.url))
        await asyncio.sleep(0)
        self._event.set()
        response = _DummyResponse()
        self.responses.append(response)
        return response


def _encode(value: str) -> str:
    return proxy_module._encode_url_part(value)


def _request(
    proxy_client,
    allowed_hosts=frozenset({"example.com"}),
    allowed_ports=frozenset({80, 443}),
):
    return SimpleNamespace(
        headers=Headers(),
        app=SimpleNamespace(
            state=SimpleNamespace(
                proxy_http_client=proxy_client,
                proxy_allowed_hosts=allowed_hosts,
                proxy_allowed_ports=allowed_ports,
            ),
        ),
    )


async def _read_streaming_response(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    if response.background is not None:
        await response.background()
    return b"".join(chunks)


@pytest.mark.anyio
async def test_proxy_awaits_async_request(anyio_backend):
    event = asyncio.Event()
    requested_urls: list[str] = []
    proxy_client = _DummyProxyClient(event, requested_urls)

    response = await proxy_module.proxy(
        _request(proxy_client),
        "https",
        _encode("example.com"),
        _encode("tracker"),
    )

    assert event.is_set(), "Proxy did not await the HTTP request"
    assert requested_urls == ["https://example.com/tracker"]
    assert response.status_code == 200
    assert await _read_streaming_response(response) == b"ok"
    assert proxy_client.responses[0].closed is True


@pytest.mark.anyio
async def test_proxy_allows_configured_port_on_allowed_hostname(anyio_backend):
    event = asyncio.Event()
    requested_urls: list[str] = []
    proxy_client = _DummyProxyClient(event, requested_urls)

    response = await proxy_module.proxy(
        _request(proxy_client, allowed_ports=frozenset({443, 8443})),
        "https",
        _encode("example.com:8443"),
        _encode("tracker"),
    )

    assert event.is_set(), "Proxy did not await the HTTP request"
    assert requested_urls == ["https://example.com:8443/tracker"]
    assert response.status_code == 200
    assert await _read_streaming_response(response) == b"ok"


@pytest.mark.anyio
async def test_proxy_rejects_port_not_in_allowlist(anyio_backend):
    event = asyncio.Event()
    proxy_client = _DummyProxyClient(event)

    with pytest.raises(proxy_module.HTTPException) as exc_info:
        await proxy_module.proxy(
            _request(proxy_client),
            "https",
            _encode("example.com:9200"),
            _encode("internal"),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Proxy target port not allowed"
    assert not event.is_set()


@pytest.mark.anyio
async def test_proxy_rejects_authority_with_allowed_userinfo_but_disallowed_host(
    anyio_backend,
):
    event = asyncio.Event()
    proxy_client = _DummyProxyClient(event)

    with pytest.raises(proxy_module.HTTPException) as exc_info:
        await proxy_module.proxy(
            _request(proxy_client, allowed_hosts=frozenset({"allowed.com"})),
            "http",
            _encode("allowed.com:80@169.254.169.254"),
            _encode("latest/meta-data"),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Proxy target not allowed"
    assert not event.is_set()


@pytest.mark.anyio
async def test_proxy_reuses_request_scoped_lifespan_client(anyio_backend):
    event = asyncio.Event()
    requested_urls: list[str] = []
    proxy_client = _DummyProxyClient(event, requested_urls)
    request = _request(proxy_client)

    first = await proxy_module.proxy(
        request,
        "https",
        _encode("example.com"),
        _encode("tracker.js"),
    )
    second = await proxy_module.proxy(
        request,
        "https",
        _encode("example.com"),
        _encode("analytics.js"),
    )

    assert await _read_streaming_response(first) == b"ok"
    assert await _read_streaming_response(second) == b"ok"
    assert requested_urls == [
        "https://example.com/tracker.js",
        "https://example.com/analytics.js",
    ]


GTAG_URL = "https://www.googletagmanager.com/gtag/js?id=G-TEST"
GTAG_ETAG = '"gtag-v1"'


def _gtag_upstream(seen: list[httpx.Request]):
    """A stand-in for the upstream host that honours ``If-None-Match``."""
    caching = {
        "Cache-Control": "private, max-age=900",
        "ETag": GTAG_ETAG,
        "Last-Modified": "Wed, 25 Nov 2026 10:00:00 GMT",
        "Expires": "Wed, 25 Nov 2026 10:15:00 GMT",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("If-None-Match") == GTAG_ETAG:
            return httpx.Response(304, headers=caching)
        return httpx.Response(
            200,
            headers={
                **caching,
                "Content-Type": "application/javascript",
                "Set-Cookie": "upstream=1",
                "X-Upstream-Internal": "leak",
            },
            content=b"// gtag",
        )

    return handler


def _proxy_app(monkeypatch, handler, env: dict[str, str] | None = None):
    """The real app with a real httpx client whose transport is ``handler``."""
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(proxy_module, "settings", Settings())
    app = build_app(monkeypatch)
    app.state.connector = RecordingConnector()
    app.state.proxy_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    app.state.proxy_allowed_hosts = frozenset({"www.googletagmanager.com"})
    app.state.proxy_allowed_ports = frozenset({80, 443})
    return app


def _route_path(url: str) -> str:
    target = httpx.URL(url)
    query = target.query.decode("ascii")
    path = target.path.removeprefix("/") + (f"?{query}" if query else "")
    return (
        f"{proxy_module.PROXY_ENDPOINT}/route/{target.scheme}"
        f"/{_encode(target.netloc.decode('ascii'))}/{_encode(path)}"
    )


def _hash(client: TestClient, headers: dict[str, str] | None = None) -> str:
    response = client.post(
        f"{proxy_module.PROXY_ENDPOINT}/hash",
        json={"url": GTAG_URL},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def test_proxy_forwards_only_allowlisted_caching_headers(monkeypatch):
    seen: list[httpx.Request] = []
    client = TestClient(_proxy_app(monkeypatch, _gtag_upstream(seen)))

    response = client.get(_route_path(GTAG_URL))

    assert response.status_code == 200
    assert response.text == "// gtag"
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "private, max-age=900"
    assert response.headers["etag"] == GTAG_ETAG
    assert response.headers["last-modified"] == "Wed, 25 Nov 2026 10:00:00 GMT"
    assert response.headers["expires"] == "Wed, 25 Nov 2026 10:15:00 GMT"
    assert "set-cookie" not in response.headers
    assert "x-upstream-internal" not in response.headers
    assert str(seen[0].url) == GTAG_URL
    assert "if-none-match" not in seen[0].headers
    assert "if-modified-since" not in seen[0].headers


def test_proxy_relays_a_304_without_a_body(monkeypatch):
    seen: list[httpx.Request] = []
    client = TestClient(_proxy_app(monkeypatch, _gtag_upstream(seen)))

    response = client.get(
        _route_path(GTAG_URL),
        headers={
            "If-None-Match": GTAG_ETAG,
            "If-Modified-Since": "Wed, 25 Nov 2026 10:00:00 GMT",
            "Cookie": "session=secret",
            "Authorization": "Bearer secret",
        },
    )

    assert response.status_code == 304
    assert response.content == b""
    assert "content-type" not in response.headers
    assert response.headers["etag"] == GTAG_ETAG
    assert response.headers["cache-control"] == "private, max-age=900"
    assert seen[0].headers["if-none-match"] == GTAG_ETAG
    assert seen[0].headers["if-modified-since"] == "Wed, 25 Nov 2026 10:00:00 GMT"
    # Only the validators travel upstream, never the visitor's credentials.
    assert "cookie" not in seen[0].headers
    assert "authorization" not in seen[0].headers


def test_proxy_serves_a_fresh_body_when_the_validator_is_stale(monkeypatch):
    seen: list[httpx.Request] = []
    client = TestClient(_proxy_app(monkeypatch, _gtag_upstream(seen)))

    response = client.get(_route_path(GTAG_URL), headers={"If-None-Match": '"old"'})

    assert response.status_code == 200
    assert response.text == "// gtag"
    assert response.headers["etag"] == GTAG_ETAG


def test_proxy_does_not_follow_an_upstream_redirect(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})

    client = TestClient(_proxy_app(monkeypatch, handler), follow_redirects=False)

    response = client.get(_route_path(GTAG_URL))

    assert response.status_code == 302
    assert "location" not in response.headers
    assert [str(request.url) for request in seen] == [GTAG_URL]


def test_proxy_conditional_request_still_checks_the_host_allowlist(monkeypatch):
    seen: list[httpx.Request] = []
    client = TestClient(_proxy_app(monkeypatch, _gtag_upstream(seen)))

    response = client.get(
        _route_path("http://169.254.169.254/latest/meta-data"),
        headers={"If-None-Match": GTAG_ETAG},
    )

    assert response.status_code == 403
    assert seen == []


def test_hash_uses_the_request_origin_when_hostname_is_not_configured(monkeypatch):
    monkeypatch.delenv("EVNT_COMMON__HOSTNAME", raising=False)
    app = _proxy_app(monkeypatch, _gtag_upstream([]))
    client = TestClient(app, base_url="http://collector.example.com:8080")

    hashed = _hash(client)

    assert hashed == f"http://collector.example.com:8080{_route_path(GTAG_URL)}"


def test_hash_honours_forwarded_proto_behind_a_trusted_proxy(monkeypatch):
    monkeypatch.delenv("EVNT_COMMON__HOSTNAME", raising=False)
    app = _proxy_app(
        monkeypatch, _gtag_upstream([]), {"EVNT_SECURITY__TRUST_PROXY_HEADERS": "true"}
    )
    client = TestClient(app, base_url="http://collector.example.com")

    hashed = _hash(
        client,
        {"X-Forwarded-Proto": "https, http", "X-Forwarded-Host": "evil.example.net"},
    )

    # The scheme comes from the proxy; the host only ever comes from Host.
    assert hashed == f"https://collector.example.com{_route_path(GTAG_URL)}"
    assert _hash(client, {"X-Forwarded-Proto": "javascript"}).startswith("http://collector.")


def test_hash_ignores_forwarded_proto_without_proxy_trust(monkeypatch):
    monkeypatch.delenv("EVNT_COMMON__HOSTNAME", raising=False)
    app = _proxy_app(
        monkeypatch, _gtag_upstream([]), {"EVNT_SECURITY__TRUST_PROXY_HEADERS": "false"}
    )
    client = TestClient(app, base_url="http://collector.example.com")

    hashed = _hash(client, {"X-Forwarded-Proto": "https"})

    assert hashed == f"http://collector.example.com{_route_path(GTAG_URL)}"


def test_hash_uses_an_explicit_hostname_whatever_the_request_says(monkeypatch):
    app = _proxy_app(
        monkeypatch,
        _gtag_upstream([]),
        {"EVNT_COMMON__HOSTNAME": "https://c.example.org:8443"},
    )
    client = TestClient(app, base_url="http://internal-service:8000")

    hashed = _hash(client, {"X-Forwarded-Proto": "http"})

    assert hashed == f"https://c.example.org:8443{_route_path(GTAG_URL)}"


def test_hashed_request_origin_url_resolves_through_the_route(monkeypatch):
    monkeypatch.delenv("EVNT_COMMON__HOSTNAME", raising=False)
    seen: list[httpx.Request] = []
    app = _proxy_app(monkeypatch, _gtag_upstream(seen))
    client = TestClient(app, base_url="http://collector.example.com")

    response = client.get(_hash(client))

    assert response.status_code == 200
    assert response.text == "// gtag"
    assert str(seen[0].url) == GTAG_URL


@pytest.mark.parametrize(
    "host",
    ["", "evil.example.com/path", "user@evil.example.com", "a b", "collector.example.com:99999"],
)
def test_public_origin_falls_back_to_the_setting_on_an_unusable_host(monkeypatch, host):
    monkeypatch.delenv("EVNT_COMMON__HOSTNAME", raising=False)
    monkeypatch.setattr(proxy_module, "settings", Settings())
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "method": "POST",
            "path": "/proxy/hash",
            "query_string": b"",
            "server": ("127.0.0.1", 8000),
            "headers": [(b"host", host.encode("latin-1"))],
        },
    )

    with capture_logs() as logs:
        origin = asyncio.run(proxy_module._public_origin(request))

    assert str(origin) == "http://localhost:8000/"
    assert [(entry["log_level"], entry["host"]) for entry in logs] == [("warning", host)]


def test_public_origin_accepts_a_bracketed_ipv6_host(monkeypatch):
    monkeypatch.delenv("EVNT_COMMON__HOSTNAME", raising=False)
    monkeypatch.setattr(proxy_module, "settings", Settings())
    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "method": "POST",
            "path": "/proxy/hash",
            "query_string": b"",
            "server": ("::1", 8443),
            "headers": [(b"host", b"[::1]:8443")],
        },
    )

    with capture_logs() as logs:
        origin = asyncio.run(proxy_module._public_origin(request))

    assert str(origin) == "https://[::1]:8443/"
    assert logs == []


def test_hash_uses_a_request_host_with_underscores(monkeypatch):
    monkeypatch.delenv("EVNT_COMMON__HOSTNAME", raising=False)
    app = _proxy_app(monkeypatch, _gtag_upstream([]))
    client = TestClient(app, base_url="http://evnt_app:8000")

    assert _hash(client) == f"http://evnt_app:8000{_route_path(GTAG_URL)}"
