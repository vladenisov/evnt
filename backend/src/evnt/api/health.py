"""Health endpoints: ``GET /`` probes the ingest backend, ``GET /live`` does not."""

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.status import HTTP_200_OK, HTTP_204_NO_CONTENT, HTTP_502_BAD_GATEWAY

from evnt.health import CachedHealthChecker
from evnt.protocols import RowSink

logger = structlog.get_logger(__name__)


class HealthProbeResponse(BaseModel):
    """JSON shape returned by the health probe endpoint."""

    status: dict[str, bool]
    healthy: bool
    ingest_mode: str
    table: str | None = None


async def liveness() -> Response:
    """Report that the event loop is responsive, without touching any backend.

    Orchestrators restart a container whose liveness probe fails, so this must
    not fail just because ClickHouse or RabbitMQ is briefly unreachable.
    """
    return Response(status_code=HTTP_204_NO_CONTENT)


async def probe(request: Request) -> HealthProbeResponse:
    """Check the active ingest backend.

    Answers 200 when every backend check passes and 502 otherwise. The
    declared return type documents the body in OpenAPI; a ``JSONResponse`` is
    returned so the status code can carry the result.
    """
    health_checker: CachedHealthChecker = request.app.state.health_checker
    connector: RowSink = request.app.state.connector
    status = await health_checker.check()
    healthy = all(status.values())

    response = HealthProbeResponse(
        status=status,
        healthy=healthy,
        ingest_mode=request.app.state.ingest_mode,
    )
    if healthy:
        try:
            response.table = await connector.get_table_name()
        except Exception as exc:
            logger.warning("Failed to get table name", error=str(exc))

    return JSONResponse(  # type: ignore[return-value]
        content=response.model_dump(exclude_none=True),
        status_code=HTTP_200_OK if healthy else HTTP_502_BAD_GATEWAY,
    )


def build_router() -> APIRouter:
    """Register the health endpoints."""
    router = APIRouter(tags=["health"])
    router.add_api_route("/", probe, methods=["GET"])
    router.add_api_route("/live", liveness, methods=["GET"], include_in_schema=False)
    return router
