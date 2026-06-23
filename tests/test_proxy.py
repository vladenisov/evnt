import asyncio
from types import SimpleNamespace

import pytest

from evnt.routers import proxy as proxy_module


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

    def build_request(self, method: str, url):
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
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
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
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
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
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
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
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
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
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
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
