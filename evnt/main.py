"""
Main application entry point for evnt.

This module creates and configures the FastAPI application with all
middleware, routers, and integrations.
"""

from asgi_correlation_id.middleware import CorrelationIdMiddleware
from brotli_asgi import BrotliMiddleware
from core.config import settings
from core.constants import APP_NAME, APP_VERSION
from core.healthcheck import liveness, probe
from core.lifespan import lifespan
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware import Middleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi_structlog.middleware import (
    AccessLogMiddleware,
    CurrentScopeSetMiddleware,
    StructlogMiddleware,
)
from middleware.security import SecurityHeadersMiddleware
from plugins.logger import init_logging, validation_exception_handler
from routers.proxy import router as proxy_router
from routers.tracker import router as app_router
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Receive, Scope, Send


class PathSkippingAccessLogMiddleware(AccessLogMiddleware):
    """Access logger that can bypass exact paths such as collector endpoints."""

    def __init__(self, app, excluded_paths: list[str] | None = None, **kwargs):
        super().__init__(app, **kwargs)
        self.excluded_paths = frozenset(excluded_paths or ())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in self.excluded_paths:
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def _get_base_middleware() -> list[Middleware]:
    """Get the list of base middleware for the application."""
    allow_all_origins = settings.security.cors_allowed_origins == ["*"]

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=[]
            if allow_all_origins
            else settings.security.cors_allowed_origins,
            allow_origin_regex=".*" if allow_all_origins else None,
            allow_credentials=settings.security.cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        ),
        Middleware(CurrentScopeSetMiddleware),
        Middleware(CorrelationIdMiddleware),
        Middleware(StructlogMiddleware),
    ]

    if settings.performance.enable_access_log:
        middleware.append(
            Middleware(
                PathSkippingAccessLogMiddleware,
                excluded_paths=settings.performance.access_log_excluded_paths,
            ),
        )

    middleware.append(Middleware(SecurityHeadersMiddleware))

    if settings.performance.enable_brotli:
        middleware.append(
            Middleware(
                BrotliMiddleware,
                excluded_handlers=settings.performance.brotli_excluded_paths,
            ),
        )

    return middleware


def _configure_conditional_middleware(app: FastAPI) -> None:
    """Add conditional middleware based on settings."""
    if settings.security.enable_https_redirect:
        app.add_middleware(HTTPSRedirectMiddleware)

    if settings.security.trusted_hosts != ["*"]:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=settings.security.trusted_hosts,
        )


def _configure_routers(app: FastAPI) -> None:
    """Configure and include all routers."""
    app.include_router(app_router)
    app.include_router(proxy_router)
    # check_dir=False so the app boots even when the static assets have not been
    # downloaded yet (they are fetched at container build time, and the dir is
    # gitignored). Requests to /static/* simply 404 until the dir is populated.
    app.mount(
        "/static",
        StaticFiles(directory="static", check_dir=False),
        name="static",
    )

    # Mount demo static assets if enabled (there are no dynamic demo routes).
    # The Vue SPA is built into routers/demo/web/dist by Vite (`npm run build`).
    if settings.common.demo:
        app.mount(
            "/demo",
            StaticFiles(directory="routers/demo/web/dist", html=True),
            name="demo",
        )


def _configure_integrations(app: FastAPI) -> None:
    """Configure optional integrations (APM, metrics, error tracking)."""
    # Add APM middleware if enabled
    if settings.elastic_apm.enabled:
        try:
            from elasticapm.contrib.starlette import ElasticAPM
            from plugins.elastic_apm import create_elastic_apm_client
        except ImportError as exc:
            raise RuntimeError(
                "Elastic APM is enabled but `elastic-apm` is not installed. "
                "Install the optional extra: `uv sync --extra apm`.",
            ) from exc

        client = create_elastic_apm_client()
        app.add_middleware(ElasticAPM, client=client)

    # Add Prometheus middleware if enabled
    if settings.prometheus.enabled:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator().instrument(app).expose(
            app,
            include_in_schema=False,
            should_gzip=True,
        )

    # Configure Sentry if enabled
    if settings.sentry.enabled:
        try:
            from fastapi_structlog.sentry import SentrySettings, setup_sentry
        except ImportError as exc:
            raise RuntimeError(
                "Sentry is enabled but `sentry-sdk` is not installed. "
                "Install the optional extra: `uv sync --extra sentry`.",
            ) from exc

        sentry_settings = SentrySettings.model_validate(
            {
                "dsn": settings.sentry.dsn,
                "environment": settings.sentry.environment,
                "traces_sample_rate": settings.sentry.traces_sample_rate,
            },
        )
        setup_sentry(
            sentry_settings,
            app_slug=settings.common.service_name,
            version=app.version,
        )


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Configure logging using fastapi-structlog
    init_logging(settings.logging.json_format, settings.logging.level)

    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url=None if settings.security.disable_docs else "/docs",
        redoc_url=None if settings.security.disable_docs else "/redoc",
        openapi_url=None if settings.security.disable_docs else "/openapi.json",
        middleware=_get_base_middleware(),
    )

    # Configure middleware, routers, and integrations
    _configure_conditional_middleware(app)

    # Add exception handler
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

    # Configure routers
    _configure_routers(app)

    # Add healthcheck endpoint
    app.add_api_route("/", probe, methods=["GET"], include_in_schema=True)
    app.add_api_route("/live", liveness, methods=["GET"], include_in_schema=False)

    # Configure integrations
    _configure_integrations(app)

    return app


app = create_app()
