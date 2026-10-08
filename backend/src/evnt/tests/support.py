"""Shared scaffolding for the HTTP-level tests.

Several suites build a real app with the production lifespan patched out and
a fake row sink. That scaffolding lives here, in a plain module, so every test
package imports the same copy.
"""

import importlib
from contextlib import asynccontextmanager

TP2_SCHEMA = "iglu:com.snowplowanalytics.snowplow/payload_data/jsonschema/1-0-4"


class RecordingConnector:
    """Fake RowSink that records the rows handed to ``insert_rows``."""

    def __init__(self):
        self.inserted_batches: list[list[dict]] = []

    async def insert_rows(self, rows, table_group: str = "evnt") -> None:
        self.inserted_batches.append(rows)

    async def get_table_name(self, table_group: str = "evnt") -> str:
        return "local"


def no_op_lifespan(_app):
    """Replace the production lifespan so no backend connection is attempted."""

    @asynccontextmanager
    async def _lifespan(_application):
        yield

    return _lifespan(_app)


def build_app(monkeypatch):
    """Build a real app with the production lifespan patched out."""
    main_module = importlib.import_module("evnt.main")
    monkeypatch.setattr(main_module, "lifespan", no_op_lifespan)
    # Importing main already configures process-wide logging. Repeated app
    # factories must not keep adding console handlers or alter pytest's capture.
    monkeypatch.setattr(main_module, "init_logging", lambda *_args: None)
    return main_module.create_app()


def minimal_tp2_payload() -> dict:
    """A minimal-but-valid Snowplow tp2 batch body (required fields only)."""
    return {
        "schema": TP2_SCHEMA,
        "data": [
            {
                "e": "pv",
                "aid": "example-app",
                "p": "web",
                "tv": "js-3.0.0",
                "res": "1920x1080",
            },
        ],
    }
