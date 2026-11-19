"""Fixtures shared by the tracker tests."""

import pytest

from evnt.tracker import payload as payload_module
from evnt.tracker import utils as utils_module


@pytest.fixture(autouse=True)
def _fresh_payload_log_limits() -> None:
    """Start each test with no rate-limited warning keys remembered.

    The limiters are module-level, so a schema logged by one test would
    otherwise be suppressed in the next one.
    """

    payload_module.reset_log_rate_limits()
    utils_module.reset_log_rate_limits()
