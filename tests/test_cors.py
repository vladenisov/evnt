import importlib
from contextlib import asynccontextmanager

from core.config import SecurityConfig
from fastapi.testclient import TestClient


def _reload_main_module():
    return importlib.import_module("evnt.main")


def _no_op_lifespan(_app):
    @asynccontextmanager
    async def _lifespan(_application):
        yield

    return _lifespan(_app)


def test_security_config_normalizes_cors_origins():
    security = SecurityConfig(
        cors_allowed_origins=["https://Example.com/"],
    )

    assert security.cors_allowed_origins == ["https://example.com"]


def test_security_config_defaults_to_open_credentialed_cors():
    security = SecurityConfig()

    assert security.cors_allowed_origins == ["*"]
    assert security.cors_allow_credentials is True


def test_security_config_allows_intentional_credentialed_wildcard():
    security = SecurityConfig(cors_allowed_origins=["*"], cors_allow_credentials=True)

    assert security.cors_allowed_origins == ["*"]
    assert security.cors_allow_credentials is True


def test_create_app_allows_credentialed_cors_for_explicit_origins(
    monkeypatch, app_root
):
    monkeypatch.chdir(app_root)
    main_module = _reload_main_module()
    monkeypatch.setattr(main_module, "lifespan", _no_op_lifespan)
    monkeypatch.setattr(
        main_module.settings.security,
        "cors_allowed_origins",
        ["https://example.com"],
    )
    monkeypatch.setattr(
        main_module.settings.security,
        "cors_allow_credentials",
        True,
    )

    app = main_module.create_app()

    with TestClient(app) as client:
        response = client.options(
            "/tracker",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://example.com"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_create_app_defaults_to_credentialed_cors_for_all_origins(
    monkeypatch, app_root
):
    monkeypatch.chdir(app_root)
    main_module = _reload_main_module()
    monkeypatch.setattr(main_module, "lifespan", _no_op_lifespan)

    app = main_module.create_app()

    with TestClient(app) as client:
        response = client.options(
            "/tracker",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://example.com"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_base_middleware_can_disable_access_log_and_brotli(monkeypatch, app_root):
    monkeypatch.chdir(app_root)
    main_module = _reload_main_module()
    monkeypatch.setattr(main_module.settings.performance, "enable_access_log", False)
    monkeypatch.setattr(main_module.settings.performance, "enable_brotli", False)

    middleware_classes = [
        middleware.cls for middleware in main_module._get_base_middleware()
    ]

    assert not any(
        issubclass(middleware_class, main_module.AccessLogMiddleware)
        for middleware_class in middleware_classes
    )
    assert main_module.BrotliMiddleware not in middleware_classes


def test_base_middleware_passes_expensive_middleware_exclusions(monkeypatch, app_root):
    monkeypatch.chdir(app_root)
    main_module = _reload_main_module()
    excluded_paths = ["/tracker", "/i"]
    monkeypatch.setattr(main_module.settings.performance, "enable_access_log", True)
    monkeypatch.setattr(main_module.settings.performance, "enable_brotli", True)
    monkeypatch.setattr(
        main_module.settings.performance,
        "access_log_excluded_paths",
        excluded_paths,
    )
    monkeypatch.setattr(
        main_module.settings.performance,
        "brotli_excluded_paths",
        excluded_paths,
    )

    middleware = main_module._get_base_middleware()
    access_log = next(
        item
        for item in middleware
        if item.cls is main_module.PathSkippingAccessLogMiddleware
    )
    brotli = next(
        item for item in middleware if item.cls is main_module.BrotliMiddleware
    )

    assert access_log.kwargs["excluded_paths"] == excluded_paths
    assert brotli.kwargs["excluded_handlers"] == excluded_paths
