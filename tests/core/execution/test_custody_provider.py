"""Original native Runtime peer adoption by Agent; no token-only substitutes."""
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
