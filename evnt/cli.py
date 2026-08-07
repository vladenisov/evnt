"""Command Line Interface for evnt.

Usage examples (from project root):

# Print settings as pretty JSON
uv run evnt/cli.py settings
# Print raw pydantic Settings object repr
uv run evnt/cli.py settings --raw
# Fire attribute traversal (prints common.hostname)
uv run evnt/cli.py settings hostname
# Create ClickHouse databases & tables
uv run evnt/cli.py db init
# Start RabbitMQ ingest worker
uv run evnt/cli.py queue worker
# Download tracker scripts (sp.js + plugins)
uv run evnt/cli.py scripts download

The `db init` command replaces the previous automatic table creation that
occurred during FastAPI lifespan startup.
"""

from __future__ import annotations

import asyncio
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
from core.config import settings
from core.constants import WORKER_LIVENESS_PATH, WORKER_LIVENESS_STALE_SECONDS
from plugins.logger import init_logging
from starlette.status import HTTP_200_OK

# ClickHouse ping query used for readiness checks
_CH_READINESS_QUERY: str = "SELECT 1"
# HTTP request timeout (seconds) for downloading tracker script bundles
_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS: int = 60


def _build_clickhouse_client_kwargs(perf_conf: Any, ch_conf: Any) -> dict[str, Any]:
    """Build async ClickHouse client kwargs shared by CLI commands."""

    return {
        **ch_conf.connection.as_client_kwargs(),
        "query_limit": 0,
        "connector_limit": perf_conf.db_pool_size,
        "connector_limit_per_host": perf_conf.db_pool_size,
    }


async def get_async_client(**kwargs: Any) -> Any:
    """Lazily create the async ClickHouse client.

    The scripts CLI does not need ClickHouse. Keeping this import lazy prevents
    static-asset downloads from requiring clickhouse-connect's async extra.
    """

    from clickhouse_connect import (  # noqa: PLC0415
        get_async_client as create_async_client,
    )

    return await create_async_client(**kwargs)


def _clickhouse_errors() -> tuple[type[Exception], type[Exception]]:
    """Return ClickHouse driver exceptions without importing them at CLI load."""

    from clickhouse_connect.driver.exceptions import (  # noqa: PLC0415
        ClickHouseError,
        DatabaseError,
    )

    return ClickHouseError, DatabaseError


def _clickhouse_connector_classes() -> tuple[type[Any], type[Any]]:
    """Return ClickHouse connector classes only for DB/worker commands."""

    from routers.tracker.db.clickhouse import (  # noqa: PLC0415
        ClickHouseConnector,
        TableManager,
    )

    return ClickHouseConnector, TableManager


def _retry_clickhouse_startup() -> Any:
    """Return the shared ClickHouse startup retry helper lazily."""

    from core.lifespan import retry_clickhouse_startup  # noqa: PLC0415

    return retry_clickhouse_startup


def _rabbitmq_batch_worker() -> type[Any]:
    """Return the RabbitMQ worker class only for queue worker startup."""

    from ingest import RabbitMQBatchWorker  # noqa: PLC0415

    return RabbitMQBatchWorker


async def connect_rabbitmq(config: Any) -> Any:
    """Lazily connect to RabbitMQ so scripts commands stay dependency-light."""

    from ingest.rabbitmq import connect_rabbitmq as create_connection  # noqa: PLC0415

    return await create_connection(config)


def _build_clickhouse_insert_settings(*, require_wait: bool = False) -> dict[str, int]:
    """Build ClickHouse insert settings for direct and worker writes."""

    direct_config = settings.ingest.direct
    if not direct_config.async_insert:
        return {}

    return {
        "async_insert": 1,
        "wait_for_async_insert": int(
            direct_config.wait_for_async_insert or require_wait,
        ),
    }


async def _check_queue_worker_dependencies() -> dict[str, bool]:
    """Check the RabbitMQ worker dependencies."""

    queue_logger = structlog.get_logger("cli.queue.healthcheck")
    perf_conf = settings.performance
    ch_conf = settings.clickhouse
    queue_conf = settings.ingest.rabbitmq

    status = {
        "clickhouse": False,
        "rabbitmq": False,
    }
    client = None
    connection = None
    channel = None

    try:
        try:
            client = await get_async_client(
                **_build_clickhouse_client_kwargs(perf_conf, ch_conf),
            )
            query = await client.query(_CH_READINESS_QUERY)
            status["clickhouse"] = query.first_row[0] == 1
        except Exception as exc:
            queue_logger.warning(
                "Worker ClickHouse health check failed",
                error=str(exc),
            )

        try:
            connection = await connect_rabbitmq(queue_conf)
            channel = await connection.channel()
            for queue_name in (
                queue_conf.queue_name,
                queue_conf.resolved_failed_queue_name,
            ):
                await channel.declare_queue(
                    queue_name,
                    durable=True,
                    passive=True,
                )
            status["rabbitmq"] = True
        except Exception as exc:
            queue_logger.warning(
                "Worker RabbitMQ health check failed",
                error=str(exc),
                queue_name=queue_conf.queue_name,
                failed_queue_name=queue_conf.resolved_failed_queue_name,
            )
    finally:
        if channel is not None:
            await channel.close()
        if connection is not None:
            await connection.close()
        if client is not None:
            await client.close()

    return status


class SettingsCommands:
    """Settings related commands.

    Exposes the pydantic Settings model through Fire. Returning native Python
    structures keeps output composable (e.g., you can pipe to jq if JSON).
    """

    def __init__(self) -> None:
        # Use cached global settings instance
        self._settings = settings

    def __call__(
        self,
        raw: bool = False,
        indent: int = 2,
    ) -> Any:  # pragma: no cover - thin wrapper
        """Print full settings.

        Parameters
        ----------
        raw : bool, default False
                If True, return the raw pydantic Settings object representation.
                Otherwise output JSON (pretty by default) so it's shell-friendly.
        indent : int, default 2
                JSON indentation (ignored when raw=True).
        """
        if raw:
            return self._settings
        return orjson.dumps(
            self._settings.model_dump(mode="json"),
            indent=indent,
            sort_keys=True,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return settings as a plain dict (no JSON serialization)."""
        return self._settings.model_dump(mode="json")

    def hostname(self) -> str:
        """Convenience accessor for common.hostname (demonstrates attribute path)."""
        return str(self._settings.common.hostname)


class DBCommands:
    """Database (ClickHouse) related commands."""

    logger = structlog.get_logger("cli.db")

    def init(self) -> str:
        """Create required ClickHouse databases and tables.

        Replaces automatic table creation previously executed during FastAPI lifespan.
        """

        async def _run():
            init_logging(settings.logging.json_format, settings.logging.level)
            perf_conf = settings.performance
            ch_conf = settings.clickhouse
            clickhouse_errors = _clickhouse_errors()
            clickhouse_connector_cls, table_manager_cls = (
                _clickhouse_connector_classes()
            )

            try:
                client = await get_async_client(
                    **_build_clickhouse_client_kwargs(perf_conf, ch_conf),
                )
            except clickhouse_errors as e:  # pragma: no cover - network path
                self.logger.error("Failed to connect to ClickHouse", error=str(e))
                raise

            try:
                connector = clickhouse_connector_cls(
                    client,
                    insert_settings=_build_clickhouse_insert_settings(),
                    **ch_conf.configuration.model_dump(),
                )
                table_manager = table_manager_cls(connector)
                await table_manager.create_all_tables()
                self.logger.info("ClickHouse initialization complete")
            finally:
                await client.close()

        asyncio.run(_run())
        return "ClickHouse initialization complete"


class CLI:
    """Root CLI object.

    Additional top-level commands can be added as new attributes or methods.
    """

    def __init__(self) -> None:  # pragma: no cover - trivial wiring
        self.settings = SettingsCommands()
        self.db = DBCommands()
        self.queue = QueueCommands()
        self.scripts = ScriptsCommands()


class ScriptsCommands:
    """Manage static tracker scripts (download/update)."""

    logger = structlog.get_logger("cli.scripts")

    def download(
        self,
        version: str = "4.6.6",
        output_dir: str = "static",
        force: bool = False,
        create_loader_copy: bool = True,
    ) -> str:
        """Download Snowplow JS tracker bundle and plugins.

        Parameters
        ----------
        version : str
                Release version tag of snowplow-javascript-tracker.
        output_dir : str
                Directory to place downloaded artifacts.
        force : bool
                Redownload even if version marker exists.
        create_loader_copy : bool
                Whether to duplicate sp.js -> loader.js (legacy naming in project).
        """
        base_url = f"https://github.com/snowplow/snowplow-javascript-tracker/releases/download/{version}"
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        marker_file = out_path / f"VERSION_{version}"
        if marker_file.exists() and not force:
            result = f"Scripts already present for version {version}. "
            result += "Use --force to redownload."
            return result

        files = ["sp.js", "sp.js.map", "plugins.umd.zip"]
        for filename in files:
            url = f"{base_url}/{filename}"
            self.logger.info("Downloading", url=url)
            resp = httpx.get(
                url, timeout=_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True,
            )
            if resp.status_code != HTTP_200_OK:
                raise RuntimeError(
                    f"Failed to download {filename}: HTTP {resp.status_code}",
                )
            (out_path / filename).write_bytes(resp.content)

        # Unzip plugins
        zip_path = out_path / "plugins.umd.zip"
        with ZipFile(BytesIO(zip_path.read_bytes())) as zf:
            zf.extractall(out_path)
        zip_path.unlink(missing_ok=True)

        # Duplicate loader copies if requested
        if create_loader_copy:
            for base in ["sp.js", "sp.js.map"]:
                src = out_path / base
                if src.exists():
                    dest = out_path / base.replace("sp.js", "loader.js")
                    dest.write_bytes(src.read_bytes())

            # Adjust source map file field so it references loader.js
            loader_map = out_path / "loader.js.map"
            if loader_map.exists():
                orig_text = loader_map.read_text(encoding="utf-8")
                data = orjson.loads(orig_text)
                data["file"] = "loader.js"
                updated_text: str = orjson.dumps(data).decode("utf-8")
                loader_map.write_text(updated_text, encoding="utf-8")
                self.logger.info("Updated loader.js.map file field to loader.js")

        # Write version marker (empty file used as flag)
        for old_marker in out_path.glob("VERSION_*"):
            if old_marker.name != marker_file.name:
                old_marker.unlink()  # clean previous markers
        marker_file.touch(exist_ok=True)

        return f"Downloaded tracker scripts version {version} to {out_path}"


class QueueCommands:
    """RabbitMQ queue commands."""

    logger = structlog.get_logger("cli.queue")

    def worker(self) -> str:
        """Start the RabbitMQ batch worker."""

        async def _run():
            init_logging(settings.logging.json_format, settings.logging.level)
            perf_conf = settings.performance
            ch_conf = settings.clickhouse
            queue_conf = settings.ingest.rabbitmq
            clickhouse_connector_cls, _ = _clickhouse_connector_classes()
            rabbitmq_batch_worker_cls = _rabbitmq_batch_worker()
            retry_clickhouse_startup = _retry_clickhouse_startup()

            async def _create_ready_client():
                client = await get_async_client(
                    **_build_clickhouse_client_kwargs(perf_conf, ch_conf),
                )
                try:
                    query = await client.query(_CH_READINESS_QUERY)
                    if query.first_row[0] != 1:
                        raise RuntimeError(
                            "ClickHouse readiness query returned unexpected result",
                        )
                    return client
                except Exception:
                    await client.close()
                    raise

            client = await retry_clickhouse_startup(
                ch_conf,
                "worker_create",
                _create_ready_client,
            )

            connector = clickhouse_connector_cls(
                client,
                insert_settings=_build_clickhouse_insert_settings(require_wait=True),
                **ch_conf.configuration.model_dump(),
            )
            worker = await rabbitmq_batch_worker_cls.create(connector, queue_conf)

            # Install a SIGTERM handler so docker/k8s shutdown cancels the
            # running task, letting the finally blocks flush and close cleanly.
            loop = asyncio.get_running_loop()
            current = asyncio.current_task()
            loop.add_signal_handler(signal.SIGTERM, current.cancel)

            try:
                await worker.run()
            except asyncio.CancelledError:
                self.logger.info("Worker received shutdown signal, flushing")
            finally:
                await worker.close()
                await client.close()

        asyncio.run(_run())
        return "RabbitMQ worker stopped"

    def _check_worker_liveness(self) -> bool:
        """Check whether the worker liveness file is present and fresh.

        The worker rewrites ``WORKER_LIVENESS_PATH`` with the wall-clock time
        after each successful flush. A missing or stale file means the worker
        loop is no longer making progress and should be considered dead.
        """

        try:
            content = WORKER_LIVENESS_PATH.read_text()
        except FileNotFoundError:
            self.logger.error(
                "Worker liveness file missing",
                liveness_path=str(WORKER_LIVENESS_PATH),
            )
            return False
        except OSError as exc:
            self.logger.error(
                "Failed to read worker liveness file",
                liveness_path=str(WORKER_LIVENESS_PATH),
                error=str(exc),
            )
            return False

        try:
            last_seen = float(content)
        except ValueError:
            self.logger.error(
                "Worker liveness file contains invalid timestamp",
                liveness_path=str(WORKER_LIVENESS_PATH),
                content=content,
            )
            return False

        age_seconds = time.time() - last_seen
        if age_seconds > WORKER_LIVENESS_STALE_SECONDS:
            self.logger.error(
                "Worker liveness file is stale",
                liveness_path=str(WORKER_LIVENESS_PATH),
                age_seconds=age_seconds,
                stale_seconds=WORKER_LIVENESS_STALE_SECONDS,
            )
            return False

        return True

    def healthcheck(self) -> str:
        """Check whether the RabbitMQ worker is alive and its deps are healthy."""

        init_logging(settings.logging.json_format, settings.logging.level)

        is_alive = self._check_worker_liveness()
        status = asyncio.run(_check_queue_worker_dependencies())
        if not is_alive or not all(status.values()):
            self.logger.error(
                "RabbitMQ worker health check failed",
                liveness=is_alive,
                status=status,
            )
            raise SystemExit(1)

        return "ok"


def main() -> None:  # pragma: no cover - Fire dispatch
    fire.Fire(CLI())


if __name__ == "__main__":  # pragma: no cover
    main()
