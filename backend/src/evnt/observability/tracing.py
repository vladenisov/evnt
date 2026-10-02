"""APM span helpers that degrade to no-ops without ``elastic-apm``.

Both helpers support the two forms the real library offers:

- decorator:        ``@async_capture_span()`` / ``@capture_span()``
- context manager:  ``async with async_capture_span("name"): ...``
                    ``with capture_span("name"): ...``
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

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
    try:
        from elasticapm.contrib.asyncio.traces import async_capture_span, capture_span
    except ImportError:

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


__all__ = ["async_capture_span", "capture_span"]
