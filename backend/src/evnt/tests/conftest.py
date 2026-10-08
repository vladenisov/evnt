"""Shared pytest fixtures for the evnt test suite."""

import pytest

from evnt.tests.support import RecordingConnector


@pytest.fixture
def anyio_backend() -> str:
    """Run ``@pytest.mark.anyio`` tests on asyncio only; the app never uses trio."""
    return "asyncio"


@pytest.fixture
def connector() -> RecordingConnector:
    """A fake RowSink that records the rows a handler forwards."""
    return RecordingConnector()
