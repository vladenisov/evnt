"""
Request body size limit for evnt.

Without a ceiling, any unauthenticated caller can make the collector buffer and
parse an arbitrarily large body, so one request can occupy a worker for as long
as it likes. This caps every endpoint uniformly; the encrypted endpoint applies
its own, much tighter limit on top.

Written as raw ASGI: the check has to count bytes as they arrive off
``receive``, which ``BaseHTTPMiddleware`` does not expose, and buffering the
body just to measure it would defeat the point.
"""

import structlog
from starlette.status import HTTP_413_CONTENT_TOO_LARGE
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from evnt.constants import CONTENT_TYPE_JSON

logger = structlog.get_logger(__name__)

_TOO_LARGE_BODY: bytes = b'{"detail":"request body too large"}'


class _BodyTooLargeError(Exception):
    """Signals that the streamed body passed the ceiling mid-flight."""


class BodySizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` with a 413."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self._declared_size_exceeds_limit(scope):
            await self._reject(scope, send, reason="content-length")
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        # A chunked body has no declared size, so the overrun is only known
        # once the app is already running. Track whether it has begun
        # responding, since a 413 can only be sent before the response starts.
        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLargeError:
            if not response_started:
                await self._reject(scope, send, reason="streamed")

    def _declared_size_exceeds_limit(self, scope: Scope) -> bool:
        """Check Content-Length so an oversized body is refused before arriving."""
        for name, value in scope.get("headers", ()):
            if name != b"content-length":
                continue
            try:
                return int(value) > self.max_bytes
            except ValueError:
                # Malformed framing; the server rejects it before we get here.
                return False
        return False

    async def _reject(self, scope: Scope, send: Send, *, reason: str) -> None:
        logger.warning(
            "Rejected oversized request body",
            path=scope.get("path"),
            limit=self.max_bytes,
            reason=reason,
        )
        await send(
            {
                "type": "http.response.start",
                "status": HTTP_413_CONTENT_TOO_LARGE,
                "headers": [
                    (b"content-type", CONTENT_TYPE_JSON.encode()),
                    (b"content-length", str(len(_TOO_LARGE_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _TOO_LARGE_BODY})
