from fastapi.testclient import TestClient

from evnt.constants import HSTS_HEADER, SECURITY_HEADERS
from evnt.tests.support import RecordingConnector, build_app


def _client(monkeypatch, *, https_redirect: bool) -> TestClient:
    import evnt.main as main_module

    monkeypatch.setattr(main_module.settings.security, "enable_https_redirect", https_redirect)
    app = build_app(monkeypatch)
    app.state.connector = RecordingConnector()
    return TestClient(app, base_url="https://testserver")


def test_every_response_carries_the_security_headers(monkeypatch):
    client = _client(monkeypatch, https_redirect=False)

    response = client.get("/live")

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "strict-transport-security" not in response.headers


def test_hsts_is_added_once_https_is_enforced(monkeypatch):
    client = _client(monkeypatch, https_redirect=True)

    response = client.get("/live")

    assert response.headers["strict-transport-security"] == HSTS_HEADER
