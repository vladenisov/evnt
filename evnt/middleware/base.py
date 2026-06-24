"""
Base middleware classes for evnt.

This module provides base classes for middleware implementations with
common functionality like request/response processing hooks and error handling.
"""

from collections.abc import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = structlog.get_logger(__name__)


class BaseMiddleware(BaseHTTPMiddleware):
    """
    Base middleware class with common functionality.

    Subclasses can override:
    - `process_request`: Called before the route handler
    - `process_response`: Called after the route handler
    - `should_process`: Determine if the middleware should process this request
    - `handle_error`: Handle exceptions during request processing
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.app = app

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        """
        Process the request and return the response.

        This method orchestrates the request processing lifecycle:
        1. Check if we should process this request
        2. Call process_request hook
        3. Call the next middleware/handler
        4. Call process_response hook
        5. Handle any errors

        Args:
            request: The incoming request
            call_next: The next handler in the chain

        Returns:
            The response from the handler chain
        """
        if not await self.should_process(request):
            return await call_next(request)

        try:
            # Pre-processing hook
            modified_request = await self.process_request(request)
            if modified_request is not None:
                # If process_request returns a Response, short-circuit
                if isinstance(modified_request, Response):
                    return modified_request

            # Call next handler, using the modified request when one was returned
            response = await call_next(
                modified_request if isinstance(modified_request, Request) else request
            )

            # Post-processing hook
            response = await self.process_response(request, response)

            return response

        except Exception as exc:
            return await self.handle_error(request, exc)

    async def should_process(self, request: Request) -> bool:
        """
        Determine if this middleware should process the request.

        Override this method to conditionally skip middleware processing.

        Args:
            request: The incoming request

        Returns:
            True if the middleware should process, False to skip
        """
        return True

    async def process_request(self, request: Request) -> Request | Response | None:
        """
        Process the incoming request before calling the next handler.

        Override this method to modify the request or short-circuit
        by returning a Response.

        Args:
            request: The incoming request

        Returns:
            None to continue, or a Response to short-circuit
        """
        return None

    async def process_response(
        self,
        request: Request,
        response: Response,
    ) -> Response:
        """
        Process the outgoing response after the handler has run.

        Override this method to modify the response.

        Args:
            request: The original request
            response: The response from the handler

        Returns:
            The (possibly modified) response
        """
        return response

    async def handle_error(self, request: Request, exc: Exception) -> Response:
        """
        Handle an exception that occurred during request processing.

        Override this method to provide custom error handling.

        Args:
            request: The request that caused the error
            exc: The exception that was raised

        Raises:
            The original exception by default
        """
        logger.error(
            "Middleware error",
            middleware=self.__class__.__name__,
            path=request.url.path,
            error=str(exc),
        )
        raise exc
