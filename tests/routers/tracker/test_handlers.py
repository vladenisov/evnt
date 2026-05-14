import importlib
import json
from ipaddress import IPv4Address

import pytest

from evnt.routers.tracker.handlers import process_data
from evnt.routers.tracker.models.snowplow import (
    PayloadElementModel,
    UserAgentModel,
)


class _RecordingLogger:
    def __init__(self):
        self.debugs = []
        self.warnings = []

    def debug(self, *args, **kwargs):
        self.debugs.append((args, kwargs))

    def warning(self, *args, **kwargs):
        self.warnings.append((args, kwargs))

    def error(self, *args, **kwargs):  # pragma: no cover - not used in this test
        raise AssertionError(f"Unexpected error log: {args!r} {kwargs!r}")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_process_data_logs_iglu_warning_but_returns_row(
    monkeypatch,
    anyio_backend,
):
    logger = _RecordingLogger()
    runtime_payload_module = importlib.import_module("routers.tracker.parsers.payload")
    monkeypatch.setattr(runtime_payload_module, "logger", logger)

    body = PayloadElementModel.model_validate(
        {
            "aid": "example-app",
            "e": "pv",
            "p": "web",
            "tv": "js-1.0.0",
            "res": "1920x1080",
            "co": json.dumps(
                {
                    "data": [
                        {
                            "schema": (
                                "iglu:com.snowplowanalytics.snowplow/"
                                "browser_context/jsonschema/2-0-0"
                            ),
                            "data": {
                                "resolution": 123,
                            },
                        },
                    ],
                },
            ),
        },
    )

    rows = await process_data(
        body=body,
        user_agent="",
        user_ip=None,
        cookies=None,
    )

    assert len(rows) == 1
    assert rows[0]["aid"] == "example-app"
    assert any(
        args[0] == "Iglu validation warning"
        and kwargs["validation_stage"] == "contexts"
        for args, kwargs in logger.warnings
    )


def test_build_initial_model_skips_revalidation_and_copies_ua_mutables(
    monkeypatch,
):
    runtime_payload_module = importlib.import_module("routers.tracker.parsers.payload")
    body = PayloadElementModel.model_validate(
        {
            "aid": "example-app",
            "e": "pv",
            "p": "web",
            "tv": "js-1.0.0",
            "lang": "ru-RU",
            "res": "1920x1080",
        },
    )
    user_agent = UserAgentModel(
        user_agent="Mozilla/5.0",
        browser_version=["123"],
        device_extra={"family": "Desktop"},
    )

    def _fail_model_validate(cls, *args, **kwargs):
        raise AssertionError("InsertModel.model_validate should not run")

    monkeypatch.setattr(
        runtime_payload_module.InsertModel,
        "model_validate",
        classmethod(_fail_model_validate),
    )

    first = runtime_payload_module._build_initial_model(
        body,
        user_agent,
        IPv4Address("127.0.0.1"),
    )
    first.browser_version.append("mutated")
    first.device_extra["family"] = "mutated"
    second = runtime_payload_module._build_initial_model(
        body,
        user_agent,
        IPv4Address("127.0.0.1"),
    )

    assert first.aid == "example-app"
    assert first.lang == "ru-RU"
    assert second.browser_version == ["123"]
    assert second.device_extra == {"family": "Desktop"}
