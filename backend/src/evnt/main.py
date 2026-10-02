"""ASGI entry point: ``uvicorn evnt.main:app``."""

from asgi_correlation_id.middleware import CorrelationIdMiddleware
from brotli_asgi import BrotliMiddleware
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware import Middleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi_structlog.middleware import CurrentScopeSetMiddleware, StructlogMiddleware
from starlette.middleware.cors import CORSMiddleware

from evnt import __version__
from evnt.api import encrypted, health, proxy, tracker
from evnt.config import Settings, settings
from evnt.constants import APP_NAME
from evnt.lifespan import lifespan
from evnt.middleware.access_log import PathSkippingAccessLogMiddleware
from evnt.middleware.body_limit import BodySizeLimitMiddleware
from evnt.middleware.security import SecurityHeadersMiddleware
from evnt.observability.logging import init_logging, validation_exception_handler


def _base_middleware(config: Settings) -> list[Middleware]:
    """Middleware passed to the constructor, outermost first."""
    allow_all_origins = config.security.cors_allowed_origins == ["*"]

    middleware = [
        Middleware(
            CORSMiddleware,
            # A literal "*" cannot be combined with credentials, so "any
            # origin" is expressed as a regex that echoes the request Origin.
            allow_origins=[] if allow_all_origins else config.security.cors_allowed_origins,
            allow_origin_regex=".*" if allow_all_origins else None,
            allow_credentials=config.security.cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        ),
        # Inside CORS so the 413 still carries the CORS headers a browser needs
        # to surface it, but ahead of everything else: an oversized body should
        # be refused before any other middleware does work on it.
        Middleware(
            BodySizeLimitMiddleware,
            max_bytes=config.security.max_request_body_bytes,
        ),
        Middleware(CurrentScopeSetMiddleware),
        Middleware(CorrelationIdMiddleware),
        Middleware(StructlogMiddleware),
    ]

    if config.performance.enable_access_log:
        middleware.append(
            Middleware(
                PathSkippingAccessLogMiddleware,
                excluded_paths=config.performance.access_log_excluded_paths,
            ),
        )

    middleware.append(Middleware(SecurityHeadersMiddleware))

    if config.performance.enable_brotli:
        middleware.append(
            Middleware(
                BrotliMiddleware,
                excluded_handlers=config.performance.brotli_excluded_paths,
            ),
        )

    return middleware


def _add_conditional_middleware(app: FastAPI, config: Settings) -> None:
    if config.security.enable_https_redirect:
        app.add_middleware(HTTPSRedirectMiddleware)

    if config.security.trusted_hosts != ["*"]:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=config.security.trusted_hosts,
        )


def _add_routes(app: FastAPI, config: Settings) -> None:
    app.include_router(health.build_router())
    app.include_router(tracker.build_router())
    app.include_router(proxy.router)
    # Opt-in: the sealed-payload endpoint is only mounted when configured, so
    # it is off the surface of deployments that do not use it.
    if config.encryption.enabled:
        app.include_router(encrypted.build_router())

    # Holds the self-hosted Snowplow tracker (`evnt scripts download`).
    # check_dir=False so the app boots before it has been downloaded; requests
    # to /static/* simply 404 until then.
    app.mount(
        "/static",
        StaticFiles(directory=config.common.static_dir, check_dir=False),
        name="static",
    )

    # The demo SPA. fallback="index.html" gives history-mode routing: deep
    # links such as /demo/tables resolve to index.html, while missing assets
    # still 404. API routes are matched first, so it never shadows them.
    if config.common.demo:
        app.frontend(
            "/demo",
            directory=config.common.demo_dir,
            fallback="index.html",
            check_dir=False,
        )


def _add_integrations(app: FastAPI, config: Settings) -> None:
    """Wire the optional APM, metrics and error-tracking integrations."""
    if config.elastic_apm.enabled:
        try:
            from elasticapm.contrib.starlette import ElasticAPM  # noqa: PLC0415

            from evnt.observability.apm import create_elastic_apm_client  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "Elastic APM is enabled but `elastic-apm` is not installed. "
                "Install the optional extra: `uv sync --extra apm`.",
            ) from exc

        app.add_middleware(ElasticAPM, client=create_elastic_apm_client())

    if config.prometheus.enabled:
        from prometheus_fastapi_instrumentator import Instrumentator  # noqa: PLC0415

        Instrumentator().instrument(app).expose(
            app,
            endpoint=config.prometheus.metrics_path,
            include_in_schema=False,
            should_gzip=True,
        )

    if config.sentry.enabled:
        try:
            from fastapi_structlog.sentry import SentrySettings, setup_sentry  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "Sentry is enabled but `sentry-sdk` is not installed. "
                "Install the optional extra: `uv sync --extra sentry`.",
            ) from exc

        setup_sentry(
            SentrySettings.model_validate(
                {
                    "dsn": config.sentry.dsn,
                    "environment": config.sentry.environment,
                    "traces_sample_rate": config.sentry.traces_sample_rate,
                },
            ),
            app_slug=config.common.service_name,
            version=app.version,
        )


def create_app() -> FastAPI:
    """Build the collector application from the current settings."""
    config = settings
    init_logging(config.logging.json_format, config.logging.level)

    docs_disabled = config.security.disable_docs
    app = FastAPI(
        title=APP_NAME,
        version=__version__,
        lifespan=lifespan,
        docs_url=None if docs_disabled else "/docs",
        redoc_url=None if docs_disabled else "/redoc",
        openapi_url=None if docs_disabled else "/openapi.json",
        middleware=_base_middleware(config),
    )
    _add_conditional_middleware(app, config)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    _add_routes(app, config)
    _add_integrations(app, config)
    return app


app = create_app()
