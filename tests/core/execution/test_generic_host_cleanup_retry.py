"""FIX-11: an unresolved native abort retains the real CLI for a channel retry."""

from __future__ import annotations

import json
import multiprocessing
import socket
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from tests.core.execution.test_generic_host_cleanup_ack import _CliProbe, _FileSignal


def _retry_cli_child(root, control, metadata, signals):
    root = Path(root)
    original_init = generic_host.LocalExecutionPreparation.__init__

    class RetryGraph:
        readiness_profile = {"verified_facts": {}}

        def __init__(self, *, request):
            self.request = request
            self.aborts = 0

        def prepare(self):
            return {"processes": {}, "engine_binding": {}, "session_config_digest": "measured",
                    "custody_capabilities": {}}

        def known_custody_capabilities(self):
            return {}

        def abort(self):
            self.aborts += 1
            (root / "graph-aborts").write_text(str(self.aborts))
            if self.aborts == 1:
                raise generic_host.CleanupUncertainError("native cleanup deadline expired; obligations retained")
            signals["retry_entered"].set()
            assert signals["retry_release"].wait(20), "native cleanup retry was never released"
            signals["cleanup_finished"].set()
            return {"engine": {"exit_code": 0}, "engine_listener": {"exit_code": 0}}

    with pytest.MonkeyPatch.context() as patch:
        # The existing native fixture retains real CLI, control framing and
        # stop handlers; its sender also gates delivery of positive cleanup.
        probe = _CliProbe(root, patch, "native_reply", signals=signals, control=control)

        def prepare(preparation, **kwargs):
            original_init(preparation, **kwargs, graph_factory=RetryGraph)
            probe.preparations.append(preparation)

        write = generic_host._write_ready_marker

        def observe_ready(path, payload):
            write(path, payload)
            if path == probe.ready and payload.get("status") == "ready":
                signals["ready_written"].set()

        patch.setattr(generic_host.LocalExecutionPreparation, "__init__", prepare)
        patch.setattr(generic_host, "_write_ready_marker", observe_ready)
        metadata.send({"request": probe.request, "abort": probe.abort, "ready": str(probe.ready)})
        metadata.close()
        probe.run()
        raise SystemExit(1 if "error" in probe.outcome else probe.outcome["result"])


def test_unresolved_native_cleanup_retains_owner_for_same_channel_retry(tmp_path):
    context = multiprocessing.get_context("spawn")
    signal_root = tmp_path / "signals"
    signal_root.mkdir()
    signals = {name: _FileSignal(signal_root / name) for name in (
        "activation", "prepared", "entered", "release", "cleanup_entered",
        "cleanup_release", "cleanup_finished", "reply_entered", "reply_release",
        "ready_written", "retry_entered", "retry_release",
    )}
    relay, control = socket.socketpair()
    relay.settimeout(20)
    received, sent = context.Pipe(duplex=False)
    process = context.Process(target=_retry_cli_child,
                              args=(str(tmp_path), control, sent, signals))
    events = []
    process.start()
    control.close()
    sent.close()
    try:
        assert received.poll(15), "native CLI did not start"
        metadata = received.recv()
        assert signals["activation"].wait(15)
        generic_host._send_activation_frame(relay, metadata["request"])
        assert generic_host._read_activation_frame(relay)["status"] == "prepared"
        signals["prepared"].set()
        assert signals["entered"].wait(15)
        signals["release"].set()
        assert signals["ready_written"].wait(15), "native CLI never published ready"
        assert json.loads(Path(metadata["ready"]).read_text())["status"] == "ready"
        events.append("ready")

        generic_host._send_activation_frame(relay, metadata["abort"])
        first = generic_host._read_activation_frame(relay)
        assert first["status"] == "unresolved", first
        assert first["error_code"] == "cleanup_unresolved", first
        assert not signals["cleanup_finished"].is_set()
        assert (tmp_path / "graph-aborts").read_text() == "1"
        process.join(timeout=0.5)
        alive_after_unresolved = process.is_alive()
        events.append({"unresolved": first["error_code"], "owner_alive": alive_after_unresolved,
                       "exit_code": process.exitcode})

        # Use the same retained socket and exact abort frame, even on the
        # failing revision, so loss of retry custody is visible in the trace.
        events.append("same_channel_retry_sent")
        generic_host._send_activation_frame(relay, metadata["abort"])
        assert alive_after_unresolved, "unresolved reply abandoned the native cleanup owner"
        assert signals["retry_entered"].wait(10), "same-channel retry did not reach native cleanup"
        assert (tmp_path / "graph-aborts").read_text() == "2"
        process.join(timeout=0.2)
        assert process.is_alive(), "CLI exited while retry cleanup was outstanding"
        assert not signals["cleanup_finished"].is_set()
        events.append("retry_cleanup_pending_owner_alive")

        signals["retry_release"].set()
        assert signals["reply_entered"].wait(10), "positive retry reply was not produced"
        assert signals["cleanup_finished"].is_set()
        process.join(timeout=0.5)
        assert process.is_alive(), "CLI exited before positive retry reply delivery"
        events.append("cleaned_reply_pending_owner_alive")
        signals["reply_release"].set()
        reply = generic_host._read_activation_frame(relay)
        assert reply["status"] == "cleaned", reply
        assert set(reply["cleanup"]) == {"engine", "engine_listener"}
        events.append("cleaned_received")

        # The release base stays a persistent worker. Stop it through its real
        # handler only after cleanup and delivery have both been observed.
        process.join(timeout=1)
        if process.is_alive():
            process.terminate()
        process.join(timeout=15)
        assert not process.is_alive(), "CLI did not finish after positive cleanup delivery"
        assert process.exitcode == 0
    finally:
        for name in ("prepared", "release", "cleanup_release", "retry_release", "reply_release"):
            signals[name].set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        events.append({"final_exit_code": process.exitcode,
                       "cleanup_finished": signals["cleanup_finished"].is_set()})
        print(json.dumps({"native_retry_events": events}, sort_keys=True))
        relay.close()
        received.close()
