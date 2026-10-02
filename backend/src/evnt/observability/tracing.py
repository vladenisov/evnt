"""APM tracing decorators with a safe fallback.

Re-exports Elastic APM span helpers when the ``elastic-apm`` package is
installed; otherwise provides pass-through no-ops so the app can run
without the APM dependency. Both shims support the two APIs the real
library offers:

- decorator form:          ``@async_capture_span()``/``@capture_span()``
- context-manager form:    ``async with async_capture_span("name"): ...``
                           ``with capture_span("name"): ...``
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    from elasticapm.contrib.asyncio.traces import (
        async_capture_span,
        capture_span,
    )
except ImportError:

    class _AsyncNoopSpan:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

        def __call__(self, func: Callable) -> Callable:
            return func

        async def __aenter__(self) -> _AsyncNoopSpan:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

    class _NoopSpan:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

        def __call__(self, func: Callable) -> Callable:
            return func

        def __enter__(self) -> _NoopSpan:
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

    async_capture_span = _AsyncNoopSpan  # type: ignore[misc,assignment]
    capture_span = _NoopSpan  # type: ignore[misc,assignment]


__all__ = ["async_capture_span", "capture_span"]
