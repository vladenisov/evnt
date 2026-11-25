"""Light CLI commands: settings, keys, tracker script download and worker liveness."""

import base64
import json
import time
from io import BytesIO
from zipfile import ZipFile

import orjson
import pytest
import structlog

import evnt.cli as cli_module
from evnt.crypto import generate_keypair


def test_settings_prints_json_and_exposes_values():
    commands = cli_module.SettingsCommands()

    dumped = json.loads(commands())

    assert dumped == commands.to_dict()
    assert dumped["common"]["hostname"] == commands.hostname()
    assert commands(raw=True) is cli_module.settings


def test_keys_generate_prints_a_usable_key_pair():
    output = cli_module.KeysCommands().generate(kid=" k9 ")

    lines = output.splitlines()
    assert lines[0] == "kid:         k9"
    public_b64 = lines[1].split()[2]
    private_b64 = lines[2].split()[2]
    assert len(base64.b64decode(public_b64)) == 32
    assert len(base64.b64decode(private_b64)) == 32
    inline = next(line for line in reversed(lines) if "EVNT_ENCRYPTION__KEYS=" in line)
    assert orjson.loads(inline.split("=", 1)[1]) == [{"kid": "k9", "private_key": private_b64}]


def test_keys_public_returns_the_public_half_of_a_configured_key(monkeypatch):
    private_b64, public_b64 = generate_keypair()
    encryption = cli_module.settings.encryption.model_validate(
        {"enabled": True, "keys": [{"kid": "k1", "private_key": private_b64}]},
    )
    monkeypatch.setattr(cli_module.settings, "encryption", encryption)

    assert cli_module.KeysCommands().public("k1") == public_b64
    with pytest.raises(ValueError, match="unknown key id 'k2'; configured: k1"):
        cli_module.KeysCommands().public("k2")


def _tracker_release(requested):
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("plugins/example.umd.js", "plugin")
    files = {
        "sp.js": b"tracker",
        "sp.js.map": orjson.dumps({"file": "sp.js", "version": 3}),
        "plugins.umd.zip": archive.getvalue(),
    }

    class _Response:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            return None

    def _get(url, timeout, follow_redirects):
        requested.append(url)
        return _Response(files[url.rsplit("/", 1)[1]])

    return _get


def test_scripts_download_writes_tracker_loader_copy_and_version_marker(monkeypatch, tmp_path):
    requested = []
    monkeypatch.setattr(cli_module.httpx, "get", _tracker_release(requested))
    (tmp_path / "VERSION_0.0.1").touch()
    commands = cli_module.ScriptsCommands()

    result = commands.download(version="9.9.9", output_dir=str(tmp_path))

    assert result == f"Downloaded tracker scripts version 9.9.9 to {tmp_path}"
    assert all("/releases/download/9.9.9/" in url for url in requested)
    assert (tmp_path / "sp.js").read_bytes() == b"tracker"
    assert (tmp_path / "loader.js").read_bytes() == b"tracker"
    assert orjson.loads((tmp_path / "loader.js.map").read_bytes())["file"] == "loader.js"
    assert orjson.loads((tmp_path / "sp.js.map").read_bytes())["file"] == "sp.js"
    assert (tmp_path / "plugins" / "example.umd.js").read_text() == "plugin"
    assert not (tmp_path / "plugins.umd.zip").exists()
    assert sorted(path.name for path in tmp_path.glob("VERSION_*")) == ["VERSION_9.9.9"]

    # Present already: nothing is fetched again unless forced.
    requested.clear()
    assert "already present" in commands.download(version="9.9.9", output_dir=str(tmp_path))
    assert requested == []
    commands.download(version="9.9.9", output_dir=str(tmp_path), force=True)
    assert len(requested) == 3


@pytest.mark.parametrize(
    ("content", "alive"),
    [
        (lambda: str(time.time()), True),
        (lambda: str(time.time() - cli_module.WORKER_LIVENESS_STALE_SECONDS - 5), False),
        (lambda: "not-a-timestamp", False),
        (lambda: None, False),
    ],
    ids=["fresh", "stale", "garbage", "missing"],
)
def test_worker_is_alive_reads_the_liveness_file(monkeypatch, tmp_path, content, alive):
    path = tmp_path / "worker.alive"
    value = content()
    if value is not None:
        path.write_text(value)
    monkeypatch.setattr(cli_module, "WORKER_LIVENESS_PATH", path)

    assert cli_module._worker_is_alive(structlog.get_logger("test")) is alive


def test_worker_is_alive_is_false_when_the_liveness_path_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module, "WORKER_LIVENESS_PATH", tmp_path)

    assert cli_module._worker_is_alive(structlog.get_logger("test")) is False


def test_queue_healthcheck_passes_only_with_fresh_liveness(monkeypatch, tmp_path):
    async def _healthy():
        return {"clickhouse": True, "rabbitmq": True}

    path = tmp_path / "worker.alive"
    monkeypatch.setattr(cli_module, "init_logging", lambda *args: None)
    monkeypatch.setattr(cli_module, "_check_queue_worker_dependencies", _healthy)
    monkeypatch.setattr(cli_module, "WORKER_LIVENESS_PATH", path)

    with pytest.raises(SystemExit) as exc_info:
        cli_module.QueueCommands().healthcheck()
    assert exc_info.value.code == 1

    path.write_text(str(time.time()))
    assert cli_module.QueueCommands().healthcheck() == "ok"
