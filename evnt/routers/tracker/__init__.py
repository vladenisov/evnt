"""
Tracker module for Snowplow event collection.

This module provides endpoints for collecting tracking data from web applications
and services using the Snowplow tracking protocol.
"""

from core.config import settings
from core.constants import CONTENT_TYPE_GIF
from fastapi.responses import Response
from fastapi.routing import APIRouter
from starlette.status import HTTP_204_NO_CONTENT

from .routes import tracker_cors, tracker_get, tracker_post  # sendgrid_event

# Get endpoint configuration from settings
endpoints = settings.common.snowplow.endpoints


router = APIRouter(tags=["tracker"])

# Register the CORS options handlers.
# Preflight responses carry no body, so return an explicit 204 No Content.
router.options(
    endpoints.post_endpoint,
    include_in_schema=False,
    status_code=HTTP_204_NO_CONTENT,
)(tracker_cors)
router.options(
    endpoints.get_endpoint,
    include_in_schema=False,
    status_code=HTTP_204_NO_CONTENT,
)(tracker_cors)

# Register the main Snowplow endpoints.
# The handler returns 204 No Content, so document that status in OpenAPI.
router.post(
    endpoints.post_endpoint,
    summary="Snowplow JS Tracker endpoint",
    status_code=HTTP_204_NO_CONTENT,
)(tracker_post)

# The GET handler returns a 1x1 GIF tracking pixel; document the binary
# image/gif response so the schema reflects the actual media type.
router.get(
    endpoints.get_endpoint,
    summary="Snowplow JS Tracker GET endpoint",
    response_class=Response,
    responses={
        200: {
            "content": {CONTENT_TYPE_GIF: {}},
            "description": "1x1 transparent GIF tracking pixel",
        },
    },
)(tracker_get)

# Register the SendGrid webhook endpoint
# Disabled: no "sendgrid" table group is registered in the ClickHouse schema
# registry, so inserts would raise KeyError. Re-enable together with schema +
# ClickHouse table definitions.
# router.post(
#     endpoints.sendgrid_endpoint,
#     summary="Sendgrid event endpoint",
# )(sendgrid_event)
