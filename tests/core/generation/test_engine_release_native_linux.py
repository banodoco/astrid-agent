"""Non-root native protocol prerequisite, not native Comfy/GPU acceptance.

This must fail as root rather than silently skipping its intended venue.
"""
import os
from pathlib import Path
import platform
import time

import pytest

from astrid.core.generation.engine_contracts import CustodyUnresolved, EndpointOwnershipError
from astrid.core.generation.engine_release_authority import process_identity
from tests.core.generation._release_fixtures import binding, claim, ready_owner, session  # noqa: F401


@pytest.mark.linux_native
def test_nonroot_original_owner_claim_and_ingress_refusal(ready_owner):
    assert platform.system() == "Linux"
    assert os.getuid() != 0 and os.geteuid() != 0, "requires a real non-root Linux user"
    state = ready_owner
    claimed = claim(state)
    assert state.engine["issuer"] != state.engine["engine"]
    assert state.engine["issuer"]["uid"] == state.engine["engine"]["uid"] == os.getuid()
    assert process_identity(state.engine["issuer"]["pid"]) == state.engine["issuer"]
    assert claimed.target["process"] == process_identity(os.getpid())
    with pytest.raises(CustodyUnresolved):
        claimed.bind(binding(state), 1, runtime=claimed.runtime,
            admission={"exclusive": True}, deadline=time.monotonic() + 5)
    with pytest.raises(EndpointOwnershipError):
        state.client.release_owned(deadline=time.monotonic() + 5)
    assert not (state.root / "release-posts").exists()
    assert Path(f"/proc/{state.engine['engine']['pid']}").exists()
    print({"uid": os.getuid(), "gid": os.getgid(), "kernel": platform.release(),
           "issuer": state.engine["issuer"], "engine": state.engine["engine"],
           "pid_namespace": os.readlink("/proc/self/ns/pid"),
           "user_namespace": os.readlink("/proc/self/ns/user")})
