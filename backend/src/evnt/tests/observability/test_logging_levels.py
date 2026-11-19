"""``init_logging`` drops disabled levels before any structlog processor runs."""

import pytest
import structlog

from evnt.observability import logging as obs


@pytest.fixture
def restore_structlog():
    config = structlog.get_config()
    yield
    structlog.configure(**config)


def test_level_filter_runs_before_every_other_processor(restore_structlog):
    obs.init_logging(enable_json_logs=True, log_level="WARNING")

    assert structlog.get_config()["processors"][0] is structlog.stdlib.filter_by_level


def test_init_logging_twice_adds_the_filter_once(restore_structlog):
    obs.init_logging(enable_json_logs=True, log_level="WARNING")
    obs.init_logging(enable_json_logs=True, log_level="WARNING")

    processors = structlog.get_config()["processors"]
    assert processors.count(structlog.stdlib.filter_by_level) == 1


def test_debug_is_dropped_at_warning_and_kept_at_debug(restore_structlog):
    seen = []

    def spy(logger, method_name, event_dict):
        seen.append(event_dict["event"])
        return event_dict

    for level, expected in (
        ("WARNING", ["kept warning"]),
        ("DEBUG", ["kept debug", "kept warning"]),
    ):
        seen.clear()
        obs.init_logging(enable_json_logs=True, log_level=level)
        processors = structlog.get_config()["processors"]
        # Right after the level filter, so it sees exactly what the filter lets through.
        structlog.configure(processors=[processors[0], spy, *processors[1:]])
        log = structlog.get_logger("evnt.test")
        log.debug("kept debug" if level == "DEBUG" else "dropped debug")
        log.warning("kept warning")
        assert seen == expected
