"""Fail-closed compatibility for the reviewed Release-0 Linux birth spelling."""

from __future__ import annotations

def birth_ids_match(recorded, observed, recorded_pid: int, observed_pid: int) -> bool:
    """Match exact IDs, or Runtime's proven R0-to-R1 Linux successor form.

    The Release-0 spelling contains merely a process start tick.  It is not
    authority by itself.  Runtime owns the legacy bridge and re-observes the
    caller-selected PID before accepting the scoped Release-1 spelling.  All
    other unrecognized, partial, unavailable, or older-Runtime observations
    fail closed.
    """
    from runtime_protocol import platform

    if (not isinstance(recorded, str) or not recorded or not isinstance(observed, str) or not observed
            or type(recorded_pid) is not int or recorded_pid <= 0
            or type(observed_pid) is not int or observed_pid != recorded_pid):
        return False
    if recorded == observed:
        return True
    try:
        return platform.legacy_linux_birth_matches_live_owner(recorded, observed, pid=recorded_pid) is True
    except (AttributeError, OSError, TypeError, ValueError):
        # A retained Runtime without the reviewed bridge, or any unavailable
        # native observation, is never an authority to signal a legacy owner.
        return False
