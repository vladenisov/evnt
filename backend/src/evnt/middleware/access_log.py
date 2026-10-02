"""Access log middleware that can skip noisy paths."""

from collections.abc import Iterable

from fastapi_structlog.middleware import AccessLogMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send


class PathSkippingAccessLogMiddleware(AccessLogMiddleware):
    """Access logger that bypasses exact paths such as the liveness probe."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        excluded_paths: Iterable[str] = (),
    ) -> None:
        super().__init__(app)
        self.excluded_paths = frozenset(excluded_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in self.excluded_paths:
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
