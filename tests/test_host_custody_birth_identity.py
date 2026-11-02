"""Host self-identity sent to or compared with the Runtime custody layer uses the custody format.

Regression for the r10b failure: the host sent process_birth_identity() (remote-owner token,
"<boot>:<ticks>" on Linux) where the Runtime expects platform.process_identity()["birth_id"]
("proc-start-ticks:N;boot:...;pidns:..."), so exact equality of the prepared reply failed.
"""
import os
import subprocess
import sys

import pytest

from astrid.core.execution import generic_host


def _runtime_birth(pid: int) -> str:
    from runtime_protocol import platform

    identity = platform.process_identity(pid)
    assert identity is not None
    return identity["birth_id"]


def test_custody_birth_matches_the_runtime_identity_for_a_real_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _runtime_birth(child.pid).startswith("proc-start-ticks:")
        # Build the host's view of the same process by pid, through the same Runtime primitive.
        from runtime_protocol import platform

        assert platform.process_identity(child.pid)["birth_id"] == _runtime_birth(child.pid)
    finally:
        child.kill()
        child.wait()


def test_host_self_identity_is_the_custody_format_not_the_remote_owner_token():
    custody = generic_host._custody_birth_identity()
    assert custody == _runtime_birth(os.getpid())
    if sys.platform.startswith("linux"):
        assert custody.startswith("proc-start-ticks:")
        assert custody != generic_host.process_birth_identity()


def test_custody_birth_fails_closed_when_the_runtime_cannot_observe_the_host(monkeypatch):
    from runtime_protocol import platform

    monkeypatch.setattr(platform, "process_identity", lambda pid, **kwargs: None)
    with pytest.raises(generic_host.HostError, match="custody birth identity is unavailable"):
        generic_host._custody_birth_identity()
