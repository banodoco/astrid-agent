"""Linux host-control peer pinning must work with a real connected peer.

Regression for the r9-r11 activation failure: the vendored custody consumer
raised "Darwin audit-token custody is unavailable" on Linux, serve_control
swallowed it and closed the control socket with the Runtime's request unread.
"""
import os
import socket
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux peer token")


def test_private_peer_pins_a_real_linux_peer(tmp_path):
    from astrid.core.execution import custody_broker

    path = tmp_path / "peer.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    child = subprocess.Popen(
        [sys.executable, "-c", "import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(5)", str(path)],
    )
    try:
        conn, _ = server.accept()
        actor = custody_broker.AuthenticatedCleanupActor.private_peer(conn)
        assert actor.verify()["pid"] == child.pid
    finally:
        child.kill()
        child.wait()
        conn.close()
        server.close()


def test_private_peer_rejects_a_changed_peer_identity_on_reuse_check(tmp_path):
    from astrid.core.execution import custody_broker

    path = tmp_path / "peer2.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    child = subprocess.Popen(
        [sys.executable, "-c", "import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(5)", str(path)],
    )
    try:
        conn, _ = server.accept()
        actor = custody_broker.AuthenticatedCleanupActor.private_peer(conn)
        child.kill(); child.wait()
        with pytest.raises(custody_broker.CustodyError):
            actor.verify()
    finally:
        conn.close()
        server.close()
