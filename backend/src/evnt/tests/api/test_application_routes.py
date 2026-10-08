"""Public routes and optional surfaces through the complete middleware stack."""

import pytest
from fastapi.testclient import TestClient

from evnt.config import settings
from evnt.tests.support import build_app, minimal_tp2_payload


@pytest.fixture(autouse=True)
def default_surfaces(monkeypatch):
    monkeypatch.setattr(settings.common, "demo", False)
    monkeypatch.setattr(settings.encryption, "enabled", False)
    monkeypatch.setattr(settings.prometheus, "enabled", False)
    monkeypatch.setattr(settings.security, "disable_docs", True)
    monkeypatch.setattr(settings.security, "trusted_hosts", ["*"])
    monkeypatch.setattr(settings.security, "enable_https_redirect", False)


@pytest.mark.parametrize(
    "path", ["/demo/", "/demo/tables", "/e", "/e.js", "/docs", "/redoc", "/openapi.json"]
)
def test_optional_routes_are_absent_by_default(monkeypatch, path):
    with TestClient(build_app(monkeypatch)) as client:
        assert client.get(path).status_code == 404


def test_opt_in_api_docs_describe_both_tracker_transports(monkeypatch):
    monkeypatch.setattr(settings.security, "disable_docs", False)
    with TestClient(build_app(monkeypatch)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert paths["/tracker"]["post"]["responses"]["204"]
    assert "image/gif" in paths["/i"]["get"]["responses"]["200"]["content"]


def test_custom_tracker_paths_replace_defaults(monkeypatch, connector):
    monkeypatch.setattr(settings.common.snowplow.endpoints, "post_endpoint", "/collect")
    monkeypatch.setattr(settings.common.snowplow.endpoints, "get_endpoint", "/pixel")
    app = build_app(monkeypatch)
    app.state.connector = connector
    payload = minimal_tp2_payload()
    with TestClient(app) as client:
        assert client.post("/collect", json=payload).status_code == 204
        assert client.get("/pixel", params=payload["data"][0]).status_code == 200
        assert client.options("/collect").status_code == 204
        assert client.options("/pixel").status_code == 204
        assert client.post("/tracker", json=payload).status_code == 404
        assert client.get("/i", params=payload["data"][0]).status_code == 404
    assert len(connector.inserted_batches) == 2


def test_configured_metrics_path_is_exposed(monkeypatch):
    monkeypatch.setattr(settings.prometheus, "enabled", True)
    monkeypatch.setattr(settings.prometheus, "metrics_path", "/internal/metrics")
    with TestClient(build_app(monkeypatch)) as client:
        assert client.get("/live").status_code == 204
        response = client.get("/internal/metrics")
        assert client.get("/metrics").status_code == 404
    assert response.status_code == 200
    assert "http_requests_total" in response.text


def test_trusted_hosts_reject_unexpected_host_before_ingest(monkeypatch, connector):
    monkeypatch.setattr(settings.security, "trusted_hosts", ["collector.example"])
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app) as client:
        rejected = client.post("/tracker", json=minimal_tp2_payload())
        accepted = client.post(
            "/tracker", headers={"Host": "collector.example"}, json=minimal_tp2_payload()
        )
    assert rejected.status_code == 400
    assert accepted.status_code == 204
    assert len(connector.inserted_batches) == 1


def test_https_redirect_preserves_the_tracker_request(monkeypatch, connector):
    monkeypatch.setattr(settings.security, "enable_https_redirect", True)
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/tracker?source=web", json=minimal_tp2_payload())
    assert response.status_code == 307
    assert response.headers["location"] == "https://testserver/tracker?source=web"
    assert connector.inserted_batches == []


@pytest.fixture
def asset_client(monkeypatch, tmp_path):
    static = tmp_path / "static"
    (static / "sp").mkdir(parents=True)
    (static / "sp" / "sp.js").write_text("window.snowplow = true;", encoding="utf-8")
    (tmp_path / "private.txt").write_text("not public", encoding="utf-8")
    monkeypatch.setattr(settings.common, "static_dir", str(static))
    with TestClient(build_app(monkeypatch)) as client:
        yield client


def test_snowplow_bundle_keeps_its_public_url_and_supports_head(asset_client):
    response = asset_client.get("/static/sp/sp.js")
    head = asset_client.head("/static/sp/sp.js")
    assert response.status_code == head.status_code == 200
    assert response.text == "window.snowplow = true;"
    assert "javascript" in response.headers["content-type"]
    assert head.content == b""
    assert head.headers["content-length"] == response.headers["content-length"]


@pytest.mark.parametrize("path", ["/static/sp/missing.js", "/static/%2e%2e/private.txt"])
def test_static_requests_cannot_escape_the_asset_directory(asset_client, path):
    assert asset_client.get(path).status_code == 404


def test_missing_static_directory_returns_404_until_scripts_are_downloaded(monkeypatch, tmp_path):
    static = tmp_path / "not-downloaded"
    monkeypatch.setattr(settings.common, "static_dir", str(static))
    with TestClient(build_app(monkeypatch)) as client:
        assert client.get("/static/sp/sp.js").status_code == 404
        (static / "sp").mkdir(parents=True)
        (static / "sp" / "sp.js").write_text("downloaded", encoding="utf-8")
        assert client.get("/static/sp/sp.js").text == "downloaded"


@pytest.fixture
def demo_client(monkeypatch, tmp_path):
    demo = tmp_path / "demo"
    (demo / "assets").mkdir(parents=True)
    (demo / "index.html").write_text("<!doctype html><title>evnt demo</title>", encoding="utf-8")
    (demo / "assets" / "app.js").write_text("console.log('demo');", encoding="utf-8")
    monkeypatch.setattr(settings.common, "demo", True)
    monkeypatch.setattr(settings.common, "demo_dir", str(demo))
    with TestClient(build_app(monkeypatch)) as client:
        yield client


@pytest.mark.parametrize("path", ["/demo/", "/demo/tables", "/demo/settings"])
def test_demo_history_routes_serve_the_index(demo_client, path):
    response = demo_client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "evnt demo" in response.text


def test_demo_assets_and_missing_assets_do_not_use_the_html_fallback(demo_client):
    response = demo_client.get("/demo/assets/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert demo_client.get("/demo/assets/missing.js").status_code == 404


@pytest.mark.parametrize("accept", ["application/json", ""])
def test_demo_history_fallback_requires_an_html_navigation(demo_client, accept):
    assert demo_client.get("/demo/tables", headers={"Accept": accept}).status_code == 404


def test_demo_does_not_shadow_collector_routes(demo_client):
    assert demo_client.get("/live").status_code == 204
    assert demo_client.get("/health").status_code == 404
    assert demo_client.get("/not-a-demo-route").status_code == 404
