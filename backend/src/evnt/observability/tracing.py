"""APM span helpers that are no-ops unless Elastic APM is enabled and installed.

The real helpers cost a wrapper call and a transaction lookup per decorated
function even with no agent running, so a collector with the ``apm`` extra
installed but ``EVNT_ELASTIC_APM__ENABLED`` off gets the no-ops too.

Both helpers support the two forms the real library offers:

- decorator:        ``@async_capture_span()`` / ``@capture_span()``
- context manager:  ``async with async_capture_span("name"): ...``
                    ``with capture_span("name"): ...``
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

from evnt.config import settings

if TYPE_CHECKING:

    class _AsyncSpan(Protocol):
        def __call__[F: Callable[..., Any]](self, func: F, /) -> F: ...

        async def __aenter__(self) -> object: ...

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
            /,
        ) -> bool | None: ...

    class _SyncSpan(Protocol):
        def __call__[F: Callable[..., Any]](self, func: F, /) -> F: ...

        def __enter__(self) -> object: ...

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
            /,
        ) -> bool | None: ...

    def async_capture_span(name: str | None = None, **kwargs: Any) -> _AsyncSpan: ...

    def capture_span(name: str | None = None, **kwargs: Any) -> _SyncSpan: ...

else:

    class _NoopSpan:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

        def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
            return func

        def __enter__(self) -> _NoopSpan:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        async def __aenter__(self) -> _NoopSpan:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    async_capture_span = _NoopSpan
    capture_span = _NoopSpan

    if settings.elastic_apm.enabled:
        # ``create_app`` reports a missing extra; spans then stay no-ops.
        with suppress(ImportError):
            from elasticapm.contrib.asyncio.traces import async_capture_span, capture_span


__all__ = ["async_capture_span", "capture_span"]
