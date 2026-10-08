"""Snowplow collector endpoints.

The paths come from settings (``common.snowplow.endpoints``) because trackers
in the field are configured with them: ``/tracker`` takes POSTed batches from
the JS and mobile trackers, ``/i`` takes single events as GET query strings and
answers with a tracking pixel.
"""

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from starlette.status import HTTP_204_NO_CONTENT

from evnt.api.deps import ClientIP, DbConnector
from evnt.config import settings
from evnt.constants import CONTENT_TYPE_GIF, TRACKING_PIXEL
from evnt.observability.tracing import async_capture_span
from evnt.tracker.models import PayloadElementModel, PayloadModel
from evnt.tracker.processing import process_data

UserAgent = Annotated[str | None, Header()]
Cookie = Annotated[str | None, Header()]


@async_capture_span()
async def tracker_cors() -> None:
    """Answer CORS preflight requests; the CORS middleware adds the headers."""


@async_capture_span()
async def tracker_post(
    connector: DbConnector,
    body: PayloadModel,
    user_ip: ClientIP,
    user_agent: UserAgent = None,
    cookie: Cookie = None,
) -> Response:
    """Store a batch POSTed by a Snowplow tracker."""
    data = await process_data(body, user_agent, user_ip, cookie)
    await connector.insert_rows(data)
    return Response(status_code=HTTP_204_NO_CONTENT)


@async_capture_span()
async def tracker_get(
    connector: DbConnector,
    params: Annotated[PayloadElementModel, Query()],
    user_ip: ClientIP,
    user_agent: UserAgent = None,
    cookie: Cookie = None,
) -> Response:
    """Store a single event sent as a query string and return the pixel."""
    data = await process_data(params, user_agent, user_ip, cookie)
    await connector.insert_rows(data)
    return Response(content=TRACKING_PIXEL, media_type=CONTENT_TYPE_GIF)


def build_router() -> APIRouter:
    """Register the collector endpoints on their configured paths."""
    endpoints = settings.common.snowplow.endpoints
    router = APIRouter(tags=["tracker"])

    # Preflight responses carry no body, so answer with an explicit 204.
    for path in (endpoints.post_endpoint, endpoints.get_endpoint):
        router.options(
            path,
            include_in_schema=False,
            status_code=HTTP_204_NO_CONTENT,
        )(tracker_cors)

    router.post(
        endpoints.post_endpoint,
        summary="Snowplow tracker POST endpoint",
        status_code=HTTP_204_NO_CONTENT,
    )(tracker_post)

    router.get(
        endpoints.get_endpoint,
        summary="Snowplow tracker GET endpoint",
        response_class=Response,
        responses={
            200: {
                "content": {CONTENT_TYPE_GIF: {}},
                "description": "1x1 transparent GIF tracking pixel",
            },
        },
    )(tracker_get)

    return router
