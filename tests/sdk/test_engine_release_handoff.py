"""Real Linux descriptor transfer, with explicit limits on mode coverage.

The execution labels here exercise correlation; they are not an authenticated
Runtime registration or proof of a supported launch mode.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from astrid.core.generation.engine_contracts import EndpointOwnershipError
from astrid.core.generation.engine_release_authority import process_identity
from astrid.sdk.engine_release_handoff import ReleaseHandoff
from astrid.sdk.host_bootstrap import PackHostBootstrapError, ensure_pack_host
from tests.core.generation._release_fixtures import issue, ready_owner, runtime, session  # noqa: F401


_RECEIVER = """
import json,socket,sys,time
from astrid.sdk.engine_release_handoff import ReleaseHandoff
channel=ReleaseHandoff(socket.socket(fileno=int(sys.argv[1])))
runtime=json.loads(sys.argv[3])
claimed=channel.claim(executor_id=runtime['executor_id'],source=sys.argv[4],runtime=runtime,
    producer=json.loads(sys.argv[2]),deadline=time.monotonic()+5)
print(json.dumps({'claimed':True,'target':claimed.target,'runtime':claimed.runtime}),flush=True)
"""


def test_inherited_descriptor_targets_real_child_and_closes_both_copies(ready_owner):
    parent, child = ReleaseHandoff.pair()
    source, execution = "sha256:" + "b" * 64, runtime()
    process = subprocess.Popen([sys.executable, "-c", _RECEIVER, str(child.fileno()),
        json.dumps(process_identity(os.getpid())), json.dumps(execution), source],
        pass_fds=(child.fileno(),), close_fds=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child.close()
    offer = issue(ready_owner)
    try:
        receipt = parent.deliver(offer, selected_process=process_identity(process.pid),
            runtime=execution, source=source, deadline=time.monotonic() + 5)
        output, error = process.communicate(timeout=5)
        assert process.returncode == 0, error
        observed = json.loads(output)
        assert observed["claimed"] is True and observed["target"]["process"]["pid"] == process.pid
        assert observed["runtime"] == execution and receipt["status"] == "claimed"
        assert parent.fileno() == child.fileno() == -1
        assert "secret" not in output and "issuance_id" not in output
        with pytest.raises(EndpointOwnershipError):
            parent.deliver(offer, selected_process=observed["target"]["process"],
                runtime=execution, source=source, deadline=time.monotonic() + 5)
    finally:
        parent.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_scm_credentials_reject_another_live_process_before_delegation(ready_owner):
    parent, child = ReleaseHandoff.pair()
    execution, source = runtime(), "sha256:" + "b" * 64
    process = subprocess.Popen([sys.executable, "-c", _RECEIVER, str(child.fileno()),
        json.dumps(process_identity(os.getpid())), json.dumps(execution), source],
        pass_fds=(child.fileno(),), close_fds=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child.close()
    offer = issue(ready_owner)
    try:
        with pytest.raises(EndpointOwnershipError, match="selected peer"):
            parent.deliver(offer, selected_process=process_identity(os.getpid()), runtime=execution,
                source=source, deadline=time.monotonic() + 5)
        assert offer._delegated is False
        assert parent.fileno() == -1
        _out, _error = process.communicate(timeout=5)
        assert process.returncode != 0
    finally:
        parent.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_partial_launch_closes_descriptor_without_minting_a_success(ready_owner):
    parent, child = ReleaseHandoff.pair()
    child.close()
    offer = issue(ready_owner)
    with pytest.raises(EndpointOwnershipError):
        parent.deliver(offer, selected_process=process_identity(os.getpid()),
            runtime=runtime(), source="source", deadline=time.monotonic() + 0.2)
    assert parent.fileno() == -1 and offer._delegated is False


def test_missing_selected_host_never_silently_drops_an_offer(ready_owner):
    offer = issue(ready_owner)
    with pytest.raises(PackHostBootstrapError, match="selected host and worker registration"):
        ensure_pack_host({}, reconfigure_action="test", release_offer=offer)
    assert offer._delegated is False


@pytest.mark.parametrize("mode", ["foreground", "macos", "windows"])
def test_unsupported_modes_refuse_before_launch(ready_owner, monkeypatch, mode):
    from astrid.sdk import host_bootstrap
    offer = issue(ready_owner)
    if mode != "foreground":
        monkeypatch.setattr(host_bootstrap.sys, "platform", "darwin" if mode == "macos" else "win32")
    with pytest.raises(PackHostBootstrapError, match="unavailable for this launch mode"):
        ensure_pack_host({}, reconfigure_action="test", release_offer=offer, _foreground=mode == "foreground")
    assert offer._delegated is False
