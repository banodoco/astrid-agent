"""The session channel deadline comes from the host run deadline, not a fixed 900 s cap."""
from __future__ import annotations

import pytest

from astrid.core.execution.generic_host_worker import SESSION_CHANNEL_MAX_TIMEOUT_SECONDS, session_channel_timeout


def test_run_deadline_over_900_seconds_is_accepted():
    assert session_channel_timeout({"run_channel_timeout_seconds": 3600.0}) == 3600.0
    assert session_channel_timeout({"run_channel_timeout_seconds": 5 * 3600.0}) == 5 * 3600.0


def test_missing_deadline_uses_the_fixed_fallback():
    from astrid.core.execution.executor.runner import SESSION_ACTION_TIMEOUT_SECONDS

    assert session_channel_timeout({}) == SESSION_ACTION_TIMEOUT_SECONDS


@pytest.mark.parametrize("value", [0, -1.0, float("inf"), True, "600", SESSION_CHANNEL_MAX_TIMEOUT_SECONDS + 1])
def test_invalid_deadline_is_refused(value):
    with pytest.raises(ValueError, match="invalid session channel deadline"):
        session_channel_timeout({"run_channel_timeout_seconds": value})
