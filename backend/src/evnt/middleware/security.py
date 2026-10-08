"""Security headers on every response.

Raw ASGI rather than ``BaseHTTPMiddleware``: the headers only need to be added
to ``http.response.start``, and the collector endpoints are hot enough that the
extra task and body buffering ``BaseHTTPMiddleware`` adds per request matter.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from evnt.config import settings
from evnt.constants import HSTS_HEADER, SECURITY_HEADERS


class SecurityHeadersMiddleware:
    """Add ``SECURITY_HEADERS`` to every HTTP response, plus HSTS behind HTTPS."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        headers = dict(SECURITY_HEADERS)
        # HSTS only makes sense once the deployment is committed to HTTPS.
        if settings.security.enable_https_redirect:
            headers["Strict-Transport-Security"] = HSTS_HEADER
        self.headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    response_headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
