"""The host must verify Runtime's real Linux designation before cleanup ACK."""
import copy
import os

import pytest

from tests.helpers.runtime import use_runtime

use_runtime()
from astrid.core.execution import custody_broker as host
from astrid_runtime import custody_broker as runtime
from runtime_protocol import linux_process_binding

pytestmark = pytest.mark.linux_native


def test_host_linux_token_matches_runtime_kernel_binding():
    assert host.current_process_audit_token(os.getpid()) == linux_process_binding.token(os.getpid())


def test_host_verifies_runtime_linux_role_reference(tmp_path):
    scope = tmp_path / "scope"
    scope.mkdir(mode=0o700)
    actor = runtime.AuthenticatedCleanupActor.current()
    identity = runtime.default_process_identity(os.getpid())
    token = linux_process_binding.token(os.getpid())
    authority = runtime.RoleCustodyAuthority(scope, "host")
    authority.designate_pending(actor=actor, identity=identity, token=token, owner_epoch="owner")
    reference = authority.bind_target(actor=actor, generation=1, identity=identity, token=token)
    verifier = host.RoleCustodyAuthority(scope, "host")
    assert verifier.verify_reference(reference, expected_actor=actor.verify(), owner_epoch="owner") == reference
    stale = copy.deepcopy(reference)
    stale["target"]["birth_id"] += "-stale"
    with pytest.raises(host.CustodyError, match="differs from selected owner/reference"):
        verifier.verify_reference(stale, expected_actor=actor.verify(), owner_epoch="owner")


def test_host_linux_token_observation_failure_stays_fenced(monkeypatch):
    def unavailable(_pid):
        raise linux_process_binding.LinuxBindingError("observation denied")

    monkeypatch.setattr(linux_process_binding, "token", unavailable)
    with pytest.raises(host.CustodyError, match="linux process identity is unavailable"):
        host.current_process_audit_token(os.getpid())


@pytest.mark.parametrize("pid", [0, -1])
def test_host_linux_invalid_process_has_no_token(pid):
    with pytest.raises(host.CustodyError, match="linux process identity is unavailable"):
        host.current_process_audit_token(pid)
