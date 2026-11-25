"""In-memory Elastic APM transport, so APM tests never open a socket.

Selected through ``ELASTIC_APM_TRANSPORT_CLASS``; lives in a plain module so
the agent's ``import_string`` and the tests resolve the same class object.
"""

import gzip
import json
from typing import Any, ClassVar

import pytest
from elasticapm.transport.base import Transport

TRANSPORT_PATH = "evnt.tests.observability.apm_support.RecordingTransport"


class RecordingTransport(Transport):
    """Keeps every event the agent would have sent to the APM server."""

    events: ClassVar[list[dict[str, Any]]] = []

    def send(self, data: bytes, forced_flush: bool = False) -> None:
        for line in gzip.decompress(data).splitlines():
            type(self).events.append(json.loads(line))

    @classmethod
    def of_type(cls, event_type: str) -> list[dict[str, Any]]:
        return [event[event_type] for event in cls.events if event_type in event]


def offline_apm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the agent at the recording transport and stop its network threads."""
    RecordingTransport.events.clear()
    monkeypatch.setenv("ELASTIC_APM_TRANSPORT_CLASS", TRANSPORT_PATH)
    monkeypatch.setenv("ELASTIC_APM_CLOUD_PROVIDER", "none")
    monkeypatch.setenv("ELASTIC_APM_CENTRAL_CONFIG", "false")
    monkeypatch.setenv("ELASTIC_APM_METRICS_INTERVAL", "0s")
    # Leave third-party libraries unpatched for the rest of the test session.
    monkeypatch.setenv("ELASTIC_APM_INSTRUMENT", "false")
