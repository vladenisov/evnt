"""Operational CLI: ``evnt <group> <command>``.

    evnt settings                  # effective settings as JSON (secrets masked)
    evnt settings hostname         # one value, via Fire attribute traversal
    evnt db init                   # create the ClickHouse database and tables
    evnt queue worker              # run the RabbitMQ -> ClickHouse batch worker
    evnt queue healthcheck         # exit 1 unless the worker is alive and healthy
    evnt scripts download          # self-host the Snowplow JS tracker
    evnt keys generate --kid=k1    # key pair for the encrypted endpoint
    evnt keys public --kid=k1      # public half of a configured key

Commands import their backends lazily so that light commands (``settings``,
``scripts``, ``keys``) start fast and do not load the ClickHouse async stack.
"""

import asyncio
import base64
import json
import signal
import time
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import fire
import httpx
import orjson
import structlog

from evnt.config import settings
from evnt.constants import WORKER_LIVENESS_PATH, WORKER_LIVENESS_STALE_SECONDS
from evnt.observability.logging import init_logging

_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS = 60
DEFAULT_TRACKER_VERSION = "4.10.2"


def _init_logging() -> None:
    init_logging(settings.logging.json_format, settings.logging.level)


async def _check_queue_worker_dependencies() -> dict[str, bool]:
    """Check that the worker can reach ClickHouse and both of its queues."""
    from evnt.ingest.rabbitmq import connect_rabbitmq  # noqa: PLC0415
    from evnt.storage.clickhouse import client as clickhouse  # noqa: PLC0415

    log = structlog.get_logger("cli.queue.healthcheck")
    queue_conf = settings.ingest.rabbitmq
    status = {"clickhouse": False, "rabbitmq": False}

    try:
        ch_client = await clickhouse.create_client(
            settings.clickhouse,
            settings.performance.db_pool_size,
        )
    except Exception as exc:
        log.warning("Worker ClickHouse health check failed", error=str(exc))
    else:
        try:
            status["clickhouse"] = await clickhouse.is_ready(ch_client)
        except Exception as exc:
            log.warning("Worker ClickHouse health check failed", error=str(exc))
        finally:
            await ch_client.close()

    try:
        connection = await connect_rabbitmq(queue_conf)
    except Exception as exc:
        log.warning("Worker RabbitMQ health check failed", error=str(exc))
        return status

    try:
        channel = await connection.channel()
        try:
            for queue_name in (queue_conf.queue_name, queue_conf.resolved_failed_queue_name):
                await channel.declare_queue(queue_name, durable=True, passive=True)
            status["rabbitmq"] = True
        finally:
            await channel.close()
    except Exception as exc:
        log.warning(
            "Worker RabbitMQ health check failed",
            error=str(exc),
            queue_name=queue_conf.queue_name,
            failed_queue_name=queue_conf.resolved_failed_queue_name,
        )
    finally:
        await connection.close()

    return status


def _worker_is_alive(log: Any) -> bool:
    """Whether the worker liveness file is present and fresh.

    The worker rewrites ``WORKER_LIVENESS_PATH`` with the wall-clock time after
    each flush and on a heartbeat. A missing or stale file means the worker
    loop has stopped making progress.
    """
    path = str(WORKER_LIVENESS_PATH)
    try:
        content = WORKER_LIVENESS_PATH.read_text()
    except FileNotFoundError:
        log.error("Worker liveness file missing", liveness_path=path)
        return False
    except OSError as exc:
        log.error("Failed to read worker liveness file", liveness_path=path, error=str(exc))
        return False

    try:
        last_seen = float(content)
    except ValueError:
        log.error(
            "Worker liveness file contains invalid timestamp",
            liveness_path=path,
            content=content,
        )
        return False

    age_seconds = time.time() - last_seen
    if age_seconds > WORKER_LIVENESS_STALE_SECONDS:
        log.error(
            "Worker liveness file is stale",
            liveness_path=path,
            age_seconds=age_seconds,
            stale_seconds=WORKER_LIVENESS_STALE_SECONDS,
        )
        return False
    return True


class SettingsCommands:
    """Inspect the effective settings."""

    def __call__(self, raw: bool = False, indent: int = 2) -> Any:
        """Print the settings as JSON with secrets masked.

        Args:
            raw: Return the pydantic Settings object instead of JSON.
            indent: JSON indentation.
        """
        if raw:
            return settings
        return json.dumps(settings.model_dump(mode="json"), indent=indent, sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        """Return the settings as a plain dict."""
        return settings.model_dump(mode="json")

    def hostname(self) -> str:
        """Return ``common.hostname``."""
        return str(settings.common.hostname)


class DBCommands:
    """ClickHouse schema management."""

    def init(self) -> str:
        """Create the ClickHouse database and tables (idempotent).

        The app never creates tables on startup, so run this once per
        deployment and again after schema changes.
        """
        from evnt.storage.clickhouse import (  # noqa: PLC0415
            ClickHouseConnector,
            TableManager,
        )
        from evnt.storage.clickhouse import client as clickhouse  # noqa: PLC0415

        log = structlog.get_logger("cli.db")

        async def run() -> None:
            ch_client = await clickhouse.create_client(
                settings.clickhouse,
                settings.performance.db_pool_size,
            )
            try:
                connector = ClickHouseConnector(
                    ch_client,
                    insert_settings=clickhouse.insert_settings(settings.ingest.direct),
                    **settings.clickhouse.configuration.model_dump(),
                )
                await TableManager(connector).create_all_tables()
            finally:
                await ch_client.close()

        _init_logging()
        asyncio.run(run())
        log.info("ClickHouse initialization complete")
        return "ClickHouse initialization complete"


class KeysCommands:
    """Key management for the encrypted ingest endpoint."""

    def generate(self, kid: str = "k1") -> str:
        """Generate an X25519 key pair for encrypted ingest.

        The private half goes into the collector config; the public half is
        embedded in the Android, iOS, and web clients.

        Args:
            kid: Key id clients will put in the envelope header.
        """
        from evnt.crypto import generate_keypair, validate_kid  # noqa: PLC0415

        kid = validate_kid(kid.strip())
        private_b64, public_b64 = generate_keypair()

        key_path = f"/run/secrets/evnt-{kid}.key"
        file_form = orjson.dumps([{"kid": kid, "private_key_file": key_path}]).decode()
        env_form = orjson.dumps([{"kid": kid, "private_key": private_b64}]).decode()

        # The file form leads because an environment variable is readable via
        # /proc/<pid>/environ, `docker inspect`, shell history, and CI logs --
        # and a leaked private key retroactively decrypts every payload ever
        # captured, since there is no forward secrecy on the recipient side.
        return "\n".join(
            [
                f"kid:         {kid}",
                f"public key:  {public_b64}   <- ship this to the clients",
                f"private key: {private_b64}   <- keep on the collector only",
                "",
                "Collector config (preferred -- mount the key as a secret):",
                f"  umask 077 && printf %s '{private_b64}' > {key_path}",
                "  EVNT_ENCRYPTION__ENABLED=true",
                f"  EVNT_ENCRYPTION__KEYS={file_form}",
                "",
                "Or inline, if you accept the key being readable in the process",
                "environment, shell history, and CI logs:",
                f"  EVNT_ENCRYPTION__KEYS={env_form}",
            ]
        )

    def public(self, kid: str) -> str:
        """Print the public key for a configured private key.

        Args:
            kid: Key id as configured in EVNT_ENCRYPTION__KEYS.
        """
        from evnt.crypto import Keyring  # noqa: PLC0415

        keyring = Keyring.from_config(settings.encryption)
        key_pair = keyring.get(kid)
        if key_pair is None:
            available = ", ".join(keyring.key_ids) or "none"
            raise ValueError(f"unknown key id {kid!r}; configured: {available}")
        return base64.b64encode(key_pair.public_key).decode()


class ScriptsCommands:
    """Self-hosted Snowplow JS tracker."""

    def download(
        self,
        version: str = DEFAULT_TRACKER_VERSION,
        output_dir: str | None = None,
        force: bool = False,
        create_loader_copy: bool = True,
    ) -> str:
        """Download the Snowplow JS tracker bundle and its plugins.

        Args:
            version: Release tag of snowplow-javascript-tracker.
            output_dir: Target directory; defaults to ``<common.static_dir>/sp``,
                which the app serves at ``/static/sp``.
            force: Download again even if this version is already present.
            create_loader_copy: Also publish sp.js as loader.js, a name ad
                blockers do not match on.
        """
        log = structlog.get_logger("cli.scripts")
        base_url = (
            f"https://github.com/snowplow/snowplow-javascript-tracker/releases/download/{version}"
        )
        out_path = Path(output_dir) if output_dir else settings.common.static_dir / "sp"
        out_path.mkdir(parents=True, exist_ok=True)
        marker_file = out_path / f"VERSION_{version}"
        if marker_file.exists() and not force:
            return f"Scripts already present for version {version}. Use --force to redownload."

        for filename in ("sp.js", "sp.js.map", "plugins.umd.zip"):
            url = f"{base_url}/{filename}"
            log.info("Downloading", url=url)
            response = httpx.get(
                url,
                timeout=_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            response.raise_for_status()
            (out_path / filename).write_bytes(response.content)

        zip_path = out_path / "plugins.umd.zip"
        with ZipFile(BytesIO(zip_path.read_bytes())) as archive:
            archive.extractall(out_path)
        zip_path.unlink(missing_ok=True)

        if create_loader_copy:
            for name in ("sp.js", "sp.js.map"):
                src = out_path / name
                if src.exists():
                    (out_path / name.replace("sp.js", "loader.js")).write_bytes(src.read_bytes())
            # The copied source map still names sp.js as its file.
            loader_map = out_path / "loader.js.map"
            if loader_map.exists():
                data = orjson.loads(loader_map.read_bytes())
                data["file"] = "loader.js"
                loader_map.write_bytes(orjson.dumps(data))

        for old_marker in out_path.glob("VERSION_*"):
            if old_marker.name != marker_file.name:
                old_marker.unlink()
        marker_file.touch(exist_ok=True)

        return f"Downloaded tracker scripts version {version} to {out_path}"


class QueueCommands:
    """RabbitMQ ingest worker."""

    def worker(self) -> str:
        """Consume the ingest queue and write batches to ClickHouse until SIGTERM."""
        from evnt.ingest import RabbitMQBatchWorker  # noqa: PLC0415
        from evnt.storage.clickhouse import ClickHouseConnector  # noqa: PLC0415
        from evnt.storage.clickhouse import client as clickhouse  # noqa: PLC0415

        log = structlog.get_logger("cli.queue")

        async def run() -> None:
            ch_client = await clickhouse.connect(
                settings.clickhouse,
                settings.performance.db_pool_size,
                "worker_create",
            )
            connector = ClickHouseConnector(
                ch_client,
                insert_settings=clickhouse.insert_settings(
                    settings.ingest.direct,
                    require_wait=True,
                ),
                **settings.clickhouse.configuration.model_dump(),
            )
            try:
                worker = await RabbitMQBatchWorker.create(connector, settings.ingest.rabbitmq)
            except BaseException:
                await ch_client.close()
                raise

            # SIGTERM (docker stop, Kubernetes) cancels the running task, so the
            # finally block below flushes the batch in hand and closes cleanly.
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)

            try:
                await worker.run()
            except asyncio.CancelledError:
                log.info("Worker received shutdown signal, flushing")
            finally:
                await worker.close()
                await ch_client.close()

        _init_logging()
        asyncio.run(run())
        return "RabbitMQ worker stopped"

    def healthcheck(self) -> str:
        """Exit with status 1 unless the worker is alive and its backends answer."""
        _init_logging()
        log = structlog.get_logger("cli.queue")

        alive = _worker_is_alive(log)
        status = asyncio.run(_check_queue_worker_dependencies())
        if not alive or not all(status.values()):
            log.error("RabbitMQ worker health check failed", liveness=alive, status=status)
            raise SystemExit(1)
        return "ok"


class CLI:
    """evnt operational commands."""

    def __init__(self) -> None:
        self.settings = SettingsCommands()
        self.db = DBCommands()
        self.queue = QueueCommands()
        self.scripts = ScriptsCommands()
        self.keys = KeysCommands()


def main() -> None:
    """Console script entry point."""
    fire.Fire(CLI(), name="evnt")


if __name__ == "__main__":
    main()
