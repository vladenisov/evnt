"""Shared pytest setup for the evnt test suite.

The application package supports two import styles in production:

- absolute imports rooted at the repo (``from evnt.routers...``);
- app-root imports used when running the app from inside ``evnt/``
  (``from core...``, ``from routers...``).

Both styles must resolve under pytest, so we put the repo root and the
``evnt/`` directory on ``sys.path`` once at collection time. Individual test
modules can then `import` either style without their own boilerplate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
APP_ROOT: Path = PROJECT_ROOT / "evnt"

# ``tests/`` itself goes on the path too, so every suite can `from support
# import ...` and share one copy of the HTTP scaffolding.
TESTS_ROOT: Path = PROJECT_ROOT / "tests"

for _path in (PROJECT_ROOT, APP_ROOT, TESTS_ROOT):
    _str = str(_path)
    if _str not in sys.path:
        sys.path.insert(0, _str)


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def app_root() -> Path:
    return APP_ROOT


@pytest.fixture
def connector():
    """A fake RowSink that records the rows a handler forwards."""
    # Deferred: ``support`` is only importable once the sys.path setup above
    # has run, which happens at module import time, after this file's own
    # import block would have executed.
    from support import RecordingConnector  # noqa: PLC0415

    return RecordingConnector()
