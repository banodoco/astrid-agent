"""Compare selected owner births using Runtime's process identity policy."""

from runtime_protocol import platform


def birth_matches_live_owner(expected: str, observed: dict, *, pid: int) -> bool:
    """Admit only a matching PID and a positive Runtime birth comparison.

    Runtime owns the narrow R0 Linux bridge, including its fresh native
    observation and boot/namespace checks. A definite mismatch must never
    reach that bridge. This comparison does not confer signal authority;
    callers retain their publication, realm, user and native signal checks.
    """
    if (type(pid) is not int or pid <= 0 or not isinstance(observed, dict)
            or type(observed.get("pid")) is not int or observed["pid"] != pid
            or not isinstance(expected, str) or not expected
            or not isinstance(observed.get("birth_id"), str) or not observed["birth_id"]):
        return False
    actual = observed["birth_id"]
    match = platform.process_birth_matches(expected, actual)
    if match is True:
        return True
    if match is not None:
        return False
    bridge = getattr(platform, "legacy_linux_birth_matches_live_owner", None)
    return callable(bridge) and bridge(expected, actual, pid=pid) is True
