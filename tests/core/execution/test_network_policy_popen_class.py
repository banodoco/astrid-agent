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


def test_pair_without_a_kernel_socketpair_works_and_is_recorded(guarded_sockets, monkeypatch):
    """asyncio's Windows event loop needs socket.socketpair() for its self-pipe."""
    socket, _, events = guarded_sockets
    monkeypatch.setattr(network_policy, "_kernel_socketpair", lambda: False)
    left, right = socket.socketpair()
    try:
        left.sendall(b"x")
        assert right.recv(1) == b"x"
        assert left.getsockname() == right.getpeername()
    finally:
        left.close()
        right.close()
    kind, fields = events[-1]
    assert kind == "tcp" and fields["detail"] == "local-socketpair" and fields["allowed"] is True
    assert sum(1 for _, f in events if f.get("detail") == "local-socketpair") == 1


@pytest.mark.parametrize("address", [("127.0.0.1", None), ("192.0.2.1", 9), ("::ffff:127.0.0.1", None)])
def test_every_other_connect_is_still_refused(guarded_sockets, monkeypatch, address):
    """There is no window: a loopback service on another port is never opened by a pair."""
    socket, _, _ = guarded_sockets
    monkeypatch.setattr(network_policy, "_kernel_socketpair", lambda: False)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = address[0], address[1] or listener.getsockname()[1]
    # The guard refuses before the operating system sees the address, so an
    # IPv4 socket is enough even for the mapped form (and needs no IPv6 here).
    client = socket.socket()
    left, right = socket.socketpair()
    try:
        with pytest.raises(network_policy.NetworkPolicyError):
            client.connect((host, port))
        with pytest.raises(network_policy.NetworkPolicyError):
            client.connect_ex((host, port))
    finally:
        for item in (client, left, right, listener):
            item.close()


def test_nested_and_concurrent_pairs_leave_no_state(guarded_sockets, monkeypatch):
    import threading

    socket, _, _ = guarded_sockets
    monkeypatch.setattr(network_policy, "_kernel_socketpair", lambda: False)
    pairs, errors = [], []

    def make():
        try:
            pairs.append(socket.socketpair())
        except Exception as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=make) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    try:
        assert not errors and len(pairs) == 8
        probe = socket.socket()
        target = socket.socket()
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        try:
            with pytest.raises(network_policy.NetworkPolicyError):
                probe.connect(target.getsockname())
        finally:
            probe.close()
            target.close()
    finally:
        for left, right in pairs:
            left.close()
            right.close()


def test_unsupported_pair_arguments_are_refused(guarded_sockets, monkeypatch):
    socket, _, _ = guarded_sockets
    monkeypatch.setattr(network_policy, "_kernel_socketpair", lambda: False)
    with pytest.raises(ValueError):
        socket.socketpair(socket.AF_INET, socket.SOCK_DGRAM)


def test_posix_socketpair_and_asyncio_are_untouched_under_the_guard(guarded_sockets):
    """With a kernel socketpair the guard must pass the call through with its own defaults."""
    import asyncio

    socket, _, events = guarded_sockets
    left, right = socket.socketpair()
    try:
        assert left.family == socket.AF_UNIX
        left.sendall(b"x")
        assert right.recv(1) == b"x"
    finally:
        left.close()
        right.close()
    loop = asyncio.new_event_loop()
    loop.close()

    async def answer():
        return 42

    assert asyncio.run(answer()) == 42
    assert not [fields for _, fields in events if fields.get("detail") == "local-socketpair"]


@pytest.mark.parametrize("program", [
    "ffprobe", "ffprobe.exe", "FFPROBE.EXE", "/usr/bin/ffprobe",
    r"C:\Program Files\ffmpeg\bin\ffprobe.exe", "ffmpeg.exe",
])
def test_local_media_tools_are_recognised_with_a_windows_suffix_or_path(program):
    assert network_policy._local_media_tool([program, "-v", "error", "clip.mp4"]) is True


@pytest.mark.parametrize("program", ["ffprobe.exe.bat", "notffprobe.exe", "ffprobe.com", "curl.exe"])
def test_other_programs_are_not_media_tools(program):
    assert network_policy._local_media_tool([program, "-v", "error", "clip.mp4"]) is False


def test_media_tool_with_a_url_is_still_refused_on_windows_names():
    assert network_policy._local_media_tool(["ffprobe.exe", "https://example.com/clip.mp4"]) is False


def test_git_identity_probe_is_recognised_as_git_exe():
    command = ["git", "rev-parse", "HEAD"]
    windows = [r"C:\Program Files\Git\cmd\git.exe", *command[1:]]
    assert network_policy._local_identity_tool(windows) == network_policy._local_identity_tool(command)
