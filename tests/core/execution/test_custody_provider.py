"""Original native Runtime peer adoption by Agent; no token-only substitutes."""
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from astrid.core.execution import custody_broker as custody
from runtime_protocol import process_binding, platform


def _reap(child):
    if child is not None:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def _verify_actor(channel, child):
    expected = process_binding.identity(child.pid)
    assert expected is not None
    with custody.AuthenticatedCleanupActor.private_peer(channel) as actor:
        pinned = actor.verify()
        assert {key: pinned[key] for key in ("pid", "uid", "birth_id")} == {
            key: expected[key] for key in ("pid", "uid", "birth_id")
        }
        # Closing the transport does not release the separately retained native
        # reference or make its original process identity transferable.
        channel.close()
        assert actor.verify() == pinned
        child.kill()
        child.wait(timeout=10)
        with pytest.raises(custody.CustodyError):
            actor.verify()
    with pytest.raises(custody.CustodyError, match="closed"):
        actor.verify()


def _posix_peer():
    child = None
    with tempfile.TemporaryDirectory(dir="/tmp") as root:
        endpoint = str(Path(root) / "peer")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(endpoint)
            listener.listen(1)
            listener.settimeout(10)
            try:
                child = subprocess.Popen([sys.executable, "-u", "-c",
                    "import socket,sys,time; s=socket.socket(socket.AF_UNIX); "
                    "s.connect(sys.argv[1]); s.sendall(b'R'); time.sleep(60)",
                    endpoint])
                channel, _ = listener.accept()
                with channel:
                    channel.settimeout(10)
                    assert channel.recv(1) == b"R"
                    _verify_actor(channel, child)
            finally:
                _reap(child)


@pytest.mark.linux_native
def test_agent_retains_original_linux_peer_and_refuses_exited_actor():
    # Requires the native peer-pidfd lane; missing authority is a failure here.
    _posix_peer()


@pytest.mark.darwin_native
def test_agent_retains_original_darwin_peer_and_refuses_exited_actor():
    _posix_peer()


@pytest.mark.windows_native
def test_agent_retains_original_windows_peer_and_refuses_exited_actor():
    from runtime_protocol import windows_handoff as windows
    listener = windows.Listener(timeout=10)
    child = channel = None
    try:
        child = subprocess.Popen([windows.python_executable(sys.executable), "-u", "-c",
            "import sys,time; from runtime_protocol import windows_handoff as w; "
            "s=w.connect(sys.argv[1], timeout=10); s.sendall(b'R'); time.sleep(60)",
            listener.endpoint], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        channel = listener.accept(platform.handoff_child_identity(child))
        assert channel.recv(1) == b"R"
        _verify_actor(channel, child)
    finally:
        if channel is not None:
            channel.close()
        listener.close()
        _reap(child)


@pytest.mark.windows_native
def test_agent_refuses_unpinned_windows_socket():
    first, second = socket.socketpair()
    with first, second:
        with pytest.raises(custody.CustodyError):
            custody.AuthenticatedCleanupActor.private_peer(first)


@pytest.mark.parametrize("backend", ["linux", "darwin", "windows"])
def test_named_retained_errors_normalized_without_swallowing_programming_fault(monkeypatch, backend):
    # Error translation only: these are not native custody witnesses.
    from runtime_protocol.darwin_process_binding import DarwinBindingError
    from runtime_protocol.windows_handoff import WindowsBindingError

    named = {"linux": process_binding.ProcessBindingError, "darwin": DarwinBindingError,
             "windows": WindowsBindingError}[backend]
    monkeypatch.setattr(process_binding, "backend", lambda: backend)

    class Broken:
        def verify(self):
            raise named("native refusal")

    with pytest.raises(custody.CustodyError) as refused:
        custody._retained_actor_call(Broken(), "verify")
    assert type(refused.value.__cause__) is named

    class Bug:
        def verify(self):
            raise RuntimeError("unexpected programming error")

    with pytest.raises(RuntimeError, match="unexpected programming error") as bug:
        custody._retained_actor_call(Bug(), "verify")
    assert type(bug.value) is RuntimeError


_HOST_CONTROL_CHILD = r"""
import json, sys
from astrid.core.execution.generic_host import LocalExecutionPreparation
s = LocalExecutionPreparation(operation_id="op", channel_id="channel")
thread = s.serve_control(int(sys.argv[1]))
thread.join(25)
assert not thread.is_alive(), "control receiver remained live"
assert s._retained_relay_actor is not None, s.control_error
assert s._retained_relay_actor._pinned["pid"] == int(sys.argv[2])
assert s.graph is None and s._request is None and s._intent is None
assert s.control_error is not None
print(json.dumps({"error": str(s.control_error),
                  "peer": s._retained_relay_actor._pinned,
                  "closed": not s._retained_relay_actor._native_close.alive}), flush=True)
"""


def _original_host_control_peer():
    from astrid.core.execution.generic_host import _read_activation_frame, _send_activation_frame

    parent, child_socket = socket.socketpair()
    child = None
    expected = process_binding.identity(os.getpid())
    assert expected is not None
    owner = {key: expected[key] for key in ("pid", "uid", "birth_id")}
    owner.update(runtime_instance_id="epoch", coordinator_epoch="epoch")
    try:
        child = platform.spawn_tree(
            [sys.executable, "-u", "-c", _HOST_CONTROL_CHILD,
             str(child_socket.fileno()), str(os.getpid())],
            pass_fds=(child_socket.fileno(),), close_fds=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        child_socket.close()
        parent.settimeout(15)
        # Actual host-control entry with a genuine original owner. A malformed
        # profile is rejected by the real preparation method, not by peer auth.
        frame = {"version": "runtime.local-execution-host/v1",
                 "command": "prepare_local_execution", "operation_id": "op",
                 "channel_id": "channel", "owner_epoch": "epoch", "runtime_owner": owner,
                 "profile": "deliberately-invalid", "custody_scope": "not-reached"}
        _send_activation_frame(parent, frame)
        reply = _read_activation_frame(parent)
        assert reply["status"] == "unresolved"
        assert reply["error_code"] == "preparation_unresolved"
        # The same inherited channel cannot claim a different creator.
        forged = {**owner, "pid": child.pid}
        assert forged["pid"] != owner["pid"]
        _send_activation_frame(parent, {**frame, "runtime_owner": forged})
        assert parent.recv(1) == b""
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 0, (stdout, stderr)
        receipt = json.loads(stdout)
        assert receipt["error"] == "local preparation is not from original Runtime peer"
        assert receipt["closed"] is True
        assert {key: receipt["peer"][key] for key in ("pid", "uid", "birth_id")} == {
            key: owner[key] for key in ("pid", "uid", "birth_id")}
    finally:
        parent.close()
        child_socket.close()
        if child is not None:
            try:
                _reap(child)
            finally:
                if sys.platform == "win32":
                    child._runtime_job.close()


@pytest.mark.linux_native
def test_linux_actual_host_control_authenticates_original_socketpair_creator():
    _original_host_control_peer()


@pytest.mark.darwin_native
def test_darwin_actual_host_control_authenticates_original_socketpair_creator():
    _original_host_control_peer()


@pytest.mark.windows_native
def test_windows_actual_host_control_adopts_original_bootstrap_owner_handle():
    _original_host_control_peer()
