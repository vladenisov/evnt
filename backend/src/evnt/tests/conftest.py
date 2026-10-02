"""Shared pytest fixtures for the evnt test suite."""

import pytest

from evnt.tests.support import RecordingConnector


@pytest.fixture
def connector() -> RecordingConnector:
    """A fake RowSink that records the rows a handler forwards."""
    return RecordingConnector()
