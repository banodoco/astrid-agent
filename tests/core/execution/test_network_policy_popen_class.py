"""The Popen guard must stay a subclassable type (astrid-dev#651).

On Windows, importing asyncio runs ``class Popen(subprocess.Popen)`` in
asyncio.windows_utils. When the guard replaced subprocess.Popen with a function,
that import raised TypeError inside every generation child process.
"""
import subprocess
import sys

import pytest

from astrid.core.execution import network_policy


@pytest.fixture
def guarded(monkeypatch):
    originals = {}
    events = []
    monkeypatch.setattr(network_policy, "_record", lambda kind, **fields: events.append((kind, fields)))
    network_policy._patch_native_descendants(originals)
    try:
        yield originals, events
    finally:
        for (owner, name), value in originals.items():
            setattr(owner, name, value)


def test_guard_is_a_subclassable_popen_type(guarded):
    originals, _ = guarded
    assert isinstance(subprocess.Popen, type)
    assert issubclass(subprocess.Popen, originals[(subprocess, "Popen")])

    class Popen(subprocess.Popen):  # what asyncio.windows_utils does at import
        pass

    assert issubclass(Popen, originals[(subprocess, "Popen")])


def test_guard_still_denies_and_allows(guarded, monkeypatch):
    _, events = guarded
    monkeypatch.setattr(network_policy, "_validated_descendant_owner", lambda: False)
    with pytest.raises(network_policy.NetworkPolicyError):
        subprocess.Popen([sys.executable, "-c", "pass"])
    assert events[-1][1]["allowed"] is False

    monkeypatch.setattr(network_policy, "_validated_descendant_owner", lambda: True)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    assert isinstance(process, subprocess.Popen)
    assert process.wait(timeout=30) == 0
    assert events[-1][1]["allowed"] is True


def test_subclass_of_the_guard_is_also_checked(guarded, monkeypatch):
    monkeypatch.setattr(network_policy, "_validated_descendant_owner", lambda: False)

    class Popen(subprocess.Popen):
        pass

    with pytest.raises(network_policy.NetworkPolicyError):
        Popen([sys.executable, "-c", "pass"])


@pytest.fixture
def guarded_sockets(monkeypatch):
    import socket

    originals = {}
    events = []
    monkeypatch.setattr(network_policy, "_record", lambda kind, **fields: events.append((kind, fields)))
    monkeypatch.setattr(network_policy, "_protocol_allowed", lambda kind: True)
    monkeypatch.setattr(network_policy, "_allowed_destination", lambda host, port: False)
    network_policy._patch_socket(originals)
    try:
        yield socket, originals, events
    finally:
        for (owner, name), value in originals.items():
            setattr(owner, name, value)


def test_windows_style_socketpair_is_allowed_but_other_loopback_connects_are_not(guarded_sockets):
    """asyncio's Windows event loop builds its self-pipe with the loopback fallback."""
    socket, originals, events = guarded_sockets
    # Force the pure-Python fallback Windows uses, under the guard.
    originals[(socket, "socketpair")] = socket._fallback_socketpair
    left, right = socket.socketpair()
    try:
        left.sendall(b"x")
        assert right.recv(1) == b"x"
    finally:
        left.close()
        right.close()
    assert events[-1][1]["detail"] == "local-socketpair" and events[-1][1]["allowed"] is True

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket()
    try:
        with pytest.raises(network_policy.NetworkPolicyError):
            client.connect(listener.getsockname())
    finally:
        client.close()
        listener.close()


def test_socketpair_exemption_does_not_cover_non_loopback(guarded_sockets):
    socket, originals, _ = guarded_sockets

    def hostile(*args, **kwargs):
        probe = socket.socket()
        try:
            probe.connect(("192.0.2.1", 9))
        finally:
            probe.close()

    originals[(socket, "socketpair")] = hostile
    with pytest.raises(network_policy.NetworkPolicyError):
        socket.socketpair()
