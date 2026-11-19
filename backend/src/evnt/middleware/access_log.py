"""Access log middleware that can skip noisy paths."""

import logging
import time
from collections.abc import Iterable

import structlog
from fastapi_structlog.middleware import AccessLogMiddleware
from fastapi_structlog.middleware.access_log import AccessInfo
from fastapi_structlog.middleware.context_scope import current_scope
from starlette.types import ASGIApp, Receive, Scope, Send

_INTERNAL_SERVER_ERROR = 500


class PathSkippingAccessLogMiddleware(AccessLogMiddleware):
    """Access logger that bypasses exact paths such as the liveness probe.

    The parent writes its line at INFO but builds it unconditionally: it
    collects every request and response header, formats a timestamp and binds
    the route details into the log context, about 20 us per request. With the
    access logger below INFO (production runs at WARNING) the line is dropped
    anyway, so the request skips that work and only keeps what the parent's
    side effects mean for other log lines: the scope context variable, the
    session binding, and, when the app raises, the route details and status
    500 that the outer error logger reports alongside the exception.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        excluded_paths: Iterable[str] = (),
    ) -> None:
        super().__init__(app)
        self.excluded_paths = frozenset(excluded_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if scope.get("path") in self.excluded_paths:
                await self.app(scope, receive, send)
                return
            # Checked per request: a level change at runtime takes effect at
            # once, and the stdlib caches the answer per logger.
            if not self.logger.isEnabledFor(logging.INFO):
                await self._call_without_access_line(scope, receive, send)
                return
        await super().__call__(scope, receive, send)

    async def _call_without_access_line(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        current_scope.set(scope)
        if self.methods and scope["method"] not in self.methods:
            await self.app(scope, receive, send)
            return
        if self.session_key and self.session_key in scope:
            structlog.contextvars.bind_contextvars(session=scope.get(self.session_key))

        start_time = time.perf_counter()
        try:
            await self.app(scope, receive, send)
        except Exception:
            self._bind_failure_context(scope, start_time)
            raise

    def _bind_failure_context(self, scope: Scope, start_time: float) -> None:
        """Bind what the parent binds on failure, for the outer exception log."""

        info = AccessInfo(
            response={"type": "http.response.start", "status": _INTERNAL_SERVER_ERROR},
            start_time=start_time,
            end_time=time.perf_counter(),
        )
        context = {}
        for key, func in self.bind_func.items():
            data = func(scope, info)  # type: ignore[arg-type]
            if data is not None:
                context[key] = data
        if context:
            structlog.contextvars.bind_contextvars(**context)
