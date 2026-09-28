"""
Tracker module for Snowplow event collection.

This module provides endpoints for collecting tracking data from web applications
and services using the Snowplow tracking protocol.
"""

from core.config import settings
from core.constants import CONTENT_TYPE_GIF, CONTENT_TYPE_JAVASCRIPT
from fastapi.responses import Response
from fastapi.routing import APIRouter
from starlette.status import HTTP_204_NO_CONTENT

from .routes import (
    encrypted_cors,
    encrypted_get,
    encrypted_post,
    encrypted_script,
    tracker_cors,
    tracker_get,
    tracker_post,
)

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


def build_encrypted_router() -> APIRouter:
    """Build the router for the encrypted twin of the endpoints above.

    A factory rather than module-level registration, because the endpoint is
    opt-in: ``create_app`` decides whether to mount it, so a deployment that
    does not seal payloads exposes no extra surface, and tests can build an app
    either way without reimporting this module.

    Both methods share one path since, unlike the plain endpoints, there is no
    legacy Snowplow path contract to honour here.
    """
    encryption = settings.encryption
    encrypted_router = APIRouter(tags=["tracker"])

    encrypted_router.options(
        encryption.endpoint,
        include_in_schema=False,
        status_code=HTTP_204_NO_CONTENT,
    )(encrypted_cors)

    encrypted_router.post(
        encryption.endpoint,
        summary="Encrypted Snowplow endpoint",
        description=(
            "Accepts a sealed Snowplow payload as raw bytes "
            "(application/octet-stream) or base64 text."
        ),
        status_code=HTTP_204_NO_CONTENT,
    )(encrypted_post)

    encrypted_router.get(
        encryption.endpoint,
        summary="Encrypted Snowplow GET endpoint",
        description="Pixel fallback carrying a base64url envelope in `d`.",
        response_class=Response,
        responses={
            200: {
                "content": {CONTENT_TYPE_GIF: {}},
                "description": "1x1 transparent GIF tracking pixel",
            },
        },
    )(encrypted_get)

    # Served from the endpoint path plus `.js` so the browser resolves the
    # collector origin from the script's own URL, with no second host to
    # configure and no key material in the page.
    encrypted_router.get(
        f"{encryption.endpoint}.js",
        summary="Browser sealer for the encrypted endpoint",
        description=(
            "JavaScript that seals Snowplow batches, served with this "
            "collector's public key and key id substituted in."
        ),
        response_class=Response,
        responses={
            200: {
                "content": {CONTENT_TYPE_JAVASCRIPT: {}},
                "description": "The browser sealing module",
            },
        },
    )(encrypted_script)

    return encrypted_router


# Register the SendGrid webhook endpoint
# Disabled: no "sendgrid" table group is registered in the ClickHouse schema
# registry, so inserts would raise KeyError. Re-enable together with schema +
# ClickHouse table definitions.
# router.post(
#     endpoints.sendgrid_endpoint,
#     summary="Sendgrid event endpoint",
# )(sendgrid_event)
