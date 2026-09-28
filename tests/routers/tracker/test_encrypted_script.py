"""Tests for the browser sealer served at ``/e.js``.

The interesting ones run the served JavaScript under Node and post what it
produces back to ``/e``. That closes the loop the mobile tickets can only close
by hand: a change to either the Python envelope or the JavaScript one fails
here instead of in a browser, which is the only place the two ever meet.
"""

import base64
import json
import os
import shutil
import subprocess
import textwrap

import pytest
from core.config import EncryptionKeyConfig, settings
from core.constants import CONTENT_TYPE_OCTET_STREAM
from core.crypto import Keyring, generate_keypair
from core.dependencies import get_db_connector
from fastapi.testclient import TestClient
from support import build_app, minimal_tp2_payload

SCRIPT_ENDPOINT = "/e.js"
ENDPOINT = "/e"
HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_UNAVAILABLE = 503
SCRIPT_ORIGIN = "https://collector.test"
# Where the unencrypted tracker would have posted, so the transport tests can
# tell a sealed request apart from one it passed through untouched.
TRACKER_ENDPOINT = f"{SCRIPT_ORIGIN}/tracker"

# Runs the served script the way a browser would -- as a classic script against
# a global object -- then seals the same payload twice, with and without the
# gzip flag, so both envelope shapes are checked against the Python opener.
NODE_HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    globalThis.location = { href: process.argv[3] };
    new Function(fs.readFileSync(process.argv[2], "utf8"))();
    const evnt = globalThis.evnt;
    const plaintext = new TextEncoder().encode(process.argv[4]);
    Promise.all([evnt.seal(plaintext), evnt.seal(plaintext, false)]).then(
      ([compressed, plain]) => {
        console.log(
          JSON.stringify({
            compressed: Buffer.from(compressed).toString("base64"),
            plain: Buffer.from(plain).toString("base64"),
            kid: evnt.kid,
            endpoint: evnt.endpoint,
            supported: true,
          }),
        );
      },
    );
    """,
)

# Drives the transport wrapper rather than the sealer: the tracker hands it
# every request it makes, so what it declines to touch matters as much as what
# it seals. `EVNT_NO_X25519` simulates a browser predating WebCrypto's curve
# support, where the agreed behaviour is to keep sending events in the clear
# rather than lose them.
NODE_TRANSPORT_HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    globalThis.location = { href: process.argv[3] };
    if (process.env.EVNT_NO_X25519) {
      const generate = crypto.subtle.generateKey.bind(crypto.subtle);
      crypto.subtle.generateKey = async (algorithm, ...rest) => {
        if (algorithm && algorithm.name === "X25519") throw new Error("unsupported");
        return generate(algorithm, ...rest);
      };
    }
    new Function(fs.readFileSync(process.argv[2], "utf8"))();
    const evnt = globalThis.evnt;
    const seen = [];
    globalThis.fetch = async (target, init) => {
      seen.push(
        typeof target === "string"
          ? {
              sealed: true,
              url: target,
              body: Buffer.from(init.body).toString("base64"),
            }
          : { sealed: false, url: target.url, method: target.method }
      );
      return new Response(null, { status: 204 });
    };
    (async () => {
      const post = new Request(process.argv[4], {
        method: "POST",
        body: process.argv[5],
      });
      await evnt.encryptedFetch(post);
      await evnt.encryptedFetch(new Request(process.argv[4], { method: "GET" }));
      console.log(JSON.stringify({ seen, supported: await evnt.supported() }));
    })();
    """,
)


requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="the cross-language interop check needs Node on PATH",
)


@pytest.fixture
def keys(monkeypatch) -> list[str]:
    """Enable encryption with two keys, so key order is observable."""
    kids = ["primary", "retired"]
    monkeypatch.setattr(settings.encryption, "enabled", True)
    monkeypatch.setattr(
        settings.encryption,
        "keys",
        [
            EncryptionKeyConfig(kid=kid, private_key=generate_keypair()[0])
            for kid in kids
        ],
    )
    return kids


@pytest.fixture
def client(monkeypatch, app_root, keys, connector) -> TestClient:
    """Build an app with encryption on, a live keyring, and a fake sink."""
    app = build_app(monkeypatch, app_root)
    app.state.keyring = Keyring.from_config(settings.encryption)
    app.dependency_overrides[get_db_connector] = lambda: connector
    return TestClient(app)


def _run_node(
    tmp_path, harness: str, script: str, *args: str, env: dict | None = None
) -> dict:
    """Run one of the Node harnesses against the served script."""
    script_path = tmp_path / "seal.js"
    script_path.write_text(script, encoding="utf-8")
    harness_path = tmp_path / "harness.cjs"
    harness_path.write_text(harness, encoding="utf-8")

    result = subprocess.run(
        [
            "node",
            str(harness_path),
            str(script_path),
            f"{SCRIPT_ORIGIN}{SCRIPT_ENDPOINT}",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **(env or {})},
    )
    return json.loads(result.stdout)


def _seal_with_node(tmp_path, script: str, payload: dict) -> dict:
    """Run the served sealer under Node and return what it produced."""
    return _run_node(tmp_path, NODE_HARNESS, script, json.dumps(payload))


class TestServing:
    def test_script_carries_the_primary_public_key(self, client):
        with client:
            response = client.get(SCRIPT_ENDPOINT)

        assert response.status_code == HTTP_OK
        assert response.headers["content-type"].startswith("text/javascript")
        assert "max-age" in response.headers["cache-control"]

        body = response.text
        assert "__EVNT_CONFIG__" not in body

        keyring = client.app.state.keyring
        expected = base64.b64encode(keyring.primary.public_key).decode()
        assert expected in body
        assert '"kid":"primary"' in body
        # The retired key must not be advertised: new clients seal to one key,
        # while the rest of the keyring only exists to open what is in flight.
        assert '"kid":"retired"' not in body

    def test_endpoint_is_relative_so_the_browser_resolves_the_origin(self, client):
        with client:
            body = client.get(SCRIPT_ENDPOINT).text

        assert '"endpoint":"/e"' in body

    def test_script_is_absent_without_a_keyring(self, monkeypatch, app_root, keys):
        app = build_app(monkeypatch, app_root)

        with TestClient(app) as unsealed:
            response = unsealed.get(SCRIPT_ENDPOINT)

        assert response.status_code == HTTP_UNAVAILABLE


@requires_node
class TestNodeInterop:
    @pytest.mark.parametrize("variant", ["plain", "compressed"])
    def test_sealed_batch_lands_in_the_same_rows_as_a_plaintext_post(
        self,
        client,
        connector,
        tmp_path,
        variant,
    ):
        payload = minimal_tp2_payload()

        with client:
            sealed = _seal_with_node(
                tmp_path, client.get(SCRIPT_ENDPOINT).text, payload
            )
            response = client.post(
                ENDPOINT,
                content=base64.b64decode(sealed[variant]),
                headers={"content-type": CONTENT_TYPE_OCTET_STREAM},
            )

        assert response.status_code == HTTP_NO_CONTENT
        rows = connector.inserted_batches[0]
        assert len(rows) == 1
        assert rows[0]["aid"] == "example-app"
        assert rows[0]["e"] == "pv"

    def test_compression_flag_is_set_only_when_compressing(
        self,
        client,
        tmp_path,
    ):
        with client:
            sealed = _seal_with_node(
                tmp_path,
                client.get(SCRIPT_ENDPOINT).text,
                minimal_tp2_payload(),
            )

        assert base64.b64decode(sealed["plain"])[5] == 0
        assert base64.b64decode(sealed["compressed"])[5] == 1

    def test_script_resolves_the_collector_origin_from_its_own_url(
        self,
        client,
        tmp_path,
    ):
        with client:
            sealed = _seal_with_node(
                tmp_path,
                client.get(SCRIPT_ENDPOINT).text,
                minimal_tp2_payload(),
            )

        assert sealed["endpoint"] == f"{SCRIPT_ORIGIN}{ENDPOINT}"
        assert sealed["kid"] == "primary"


@requires_node
class TestTransport:
    def test_only_event_batches_are_sealed(self, client, tmp_path):
        with client:
            result = _run_node(
                tmp_path,
                NODE_TRANSPORT_HARNESS,
                client.get(SCRIPT_ENDPOINT).text,
                TRACKER_ENDPOINT,
                json.dumps(minimal_tp2_payload()),
            )

        post, get = result["seen"]
        assert result["supported"] is True
        assert post["sealed"] is True
        assert post["url"] == f"{SCRIPT_ORIGIN}{ENDPOINT}"
        # The tracker routes its idService GET through the same hook; sealing it
        # would send the collector a body it has no way to read.
        assert get["sealed"] is False
        assert get["method"] == "GET"

    def test_a_sealed_batch_from_the_transport_still_opens(
        self,
        client,
        connector,
        tmp_path,
    ):
        with client:
            result = _run_node(
                tmp_path,
                NODE_TRANSPORT_HARNESS,
                client.get(SCRIPT_ENDPOINT).text,
                TRACKER_ENDPOINT,
                json.dumps(minimal_tp2_payload()),
            )
            response = client.post(
                ENDPOINT,
                content=base64.b64decode(result["seen"][0]["body"]),
                headers={"content-type": CONTENT_TYPE_OCTET_STREAM},
            )

        assert response.status_code == HTTP_NO_CONTENT
        assert connector.inserted_batches[0][0]["e"] == "pv"

    def test_a_browser_without_x25519_keeps_sending_in_the_clear(
        self,
        client,
        tmp_path,
    ):
        with client:
            result = _run_node(
                tmp_path,
                NODE_TRANSPORT_HARNESS,
                client.get(SCRIPT_ENDPOINT).text,
                TRACKER_ENDPOINT,
                json.dumps(minimal_tp2_payload()),
                env={"EVNT_NO_X25519": "1"},
            )

        assert result["supported"] is False
        # Both requests go out untouched, to the tracker's own endpoint: losing
        # events on an old browser would be a worse trade than not sealing them.
        assert [entry["sealed"] for entry in result["seen"]] == [False, False]
        assert result["seen"][0]["method"] == "POST"
        assert result["seen"][0]["url"] == TRACKER_ENDPOINT
