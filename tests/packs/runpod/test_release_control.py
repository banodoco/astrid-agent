"""Focused tests for the retained-owner local release-control transport."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest


def test_release_control_authenticates_and_hands_off_exact_identity(tmp_path):
    from astrid.packs.runpod import release_control

    claim = tmp_path / "claim.json"
    calls = []

    def release(operation_id: str, pod_id: str):
        if (operation_id, pod_id) != ("a" * 32, "pod-exact"):
            raise ValueError("private provider detail")
        calls.append((operation_id, pod_id))
        return {"state": "cleanup_complete"}

    server = release_control.ReleaseControlServer(claim, "epoch-1", release)
    try:
        result = release_control.request_release(claim, "a" * 32, "pod-exact")
        assert result == {"state": "cleanup_complete"}
        assert calls == [("a" * 32, "pod-exact")]

        # The request is exact-id gated by the owner callback, and callback
        # detail is not copied into a response to the local caller.
        with pytest.raises(RuntimeError, match="release request failed") as caught:
            release_control.request_release(claim, "b" * 32, "pod-other")
        assert "private provider detail" not in str(caught.value)
        assert calls == [("a" * 32, "pod-exact")]
    finally:
        server.close()
    assert not release_control.descriptor_path(claim).exists()


def test_release_control_rejects_bad_nonce_and_closes_listener(tmp_path):
    from astrid.packs.runpod import release_control

    claim = tmp_path / "claim.json"
    calls = []
    server = release_control.ReleaseControlServer(claim, "epoch-2", lambda *args: calls.append(args))
    descriptor = json.loads(release_control.descriptor_path(claim).read_text(encoding="utf-8"))
    try:
        with socket.create_connection((descriptor["host"], descriptor["port"]), timeout=3) as connection:
            release_control._send_json(connection, {
                "schema": release_control._SCHEMA,
                "nonce": "wrong",
                "owner_epoch": descriptor["owner_epoch"],
                "operation_id": "c" * 32,
                "pod_id": "pod-exact",
            })
            response = release_control._recv_json(connection)
        assert response == {"ok": False, "error": "release request failed (ValueError)"}
        assert calls == []
    finally:
        server.close()
    assert not release_control.descriptor_path(claim).exists()
    with pytest.raises(OSError):
        socket.create_connection((descriptor["host"], descriptor["port"]), timeout=1)


def test_release_control_start_failure_removes_descriptor_and_closes_socket(tmp_path, monkeypatch):
    from astrid.packs.runpod import release_control

    claim = tmp_path / "claim.json"
    created = []
    original_socket = release_control.socket.socket

    def tracked_socket(*args, **kwargs):
        instance = original_socket(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(release_control.socket, "socket", tracked_socket)
    monkeypatch.setattr(release_control.threading.Thread, "start",
                        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start failed")))
    with pytest.raises(RuntimeError, match="thread start failed"):
        release_control.ReleaseControlServer(claim, "epoch-3", lambda *_: {})
    assert created and created[0].fileno() == -1
    assert not release_control.descriptor_path(claim).exists()


def test_release_control_publication_failure_does_not_close_a_reused_descriptor(tmp_path, monkeypatch):
    import os

    from astrid.packs.runpod import release_control

    claim = tmp_path / "claim.json"
    reused = []

    def refuse_publication(*args):
        # Publishing follows closing the temporary file. Another host thread
        # may reuse that descriptor number before publication fails.
        reused.append(os.open(os.devnull, os.O_RDONLY))
        raise OSError("descriptor publication refused")

    monkeypatch.setattr(release_control.os, "replace", refuse_publication)
    try:
        with pytest.raises(OSError, match="descriptor publication refused"):
            release_control.ReleaseControlServer(claim, "epoch-4", lambda *_: {})
        assert reused
        os.fstat(reused[0])
    finally:
        for descriptor in reused:
            try:
                os.close(descriptor)
            except OSError:
                pass


@pytest.mark.parametrize("phase", ["header", "payload"])
def test_release_control_drops_slow_frame_then_serves_legitimate_release(tmp_path, phase):
    from astrid.packs.runpod import release_control

    claim = tmp_path / "claim.json"
    calls = []
    server = release_control.ReleaseControlServer(
        claim, "epoch-slow", lambda *identity: calls.append(identity) or {"state": "cleanup_complete"})
    descriptor = json.loads(release_control.descriptor_path(claim).read_text(encoding="utf-8"))
    stop = threading.Event()

    with socket.create_connection((descriptor["host"], descriptor["port"]), timeout=3.5) as peer:
        def drip():
            try:
                header = struct.pack("!I", 4096)
                for byte in header:
                    peer.sendall(bytes([byte]))
                    if stop.wait(0.75 if phase == "header" else 0.3):
                        return
                while not stop.is_set():
                    peer.sendall(b" ")
                    stop.wait(0.15)
            except OSError:
                pass

        started = time.monotonic()
        sender = threading.Thread(target=drip, daemon=True)
        sender.start()
        try:
            # Each byte arrives inside the old two-second per-recv timeout.
            # One deadline must cover both the header and the payload.
            response = release_control._recv_json(peer)
            assert time.monotonic() - started < 3.0
            assert response == {"ok": False, "error": "release request failed (TimeoutError)"}
            assert calls == []
            assert release_control.request_release(claim, "d" * 32, "pod-after-slow") == {
                "state": "cleanup_complete"}
            assert calls == [("d" * 32, "pod-after-slow")]
        finally:
            stop.set()
            sender.join(timeout=1)
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            server.close()
