"""FIX-11: real CLI/control ordering, with Runtime and native graph doubles."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash
from astrid.core.execution import custody_broker, generic_host
from tests.test_generic_host import FakeRuntime, _local_custody_request, _write_manifest


class _CliProbe:
    def __init__(self, root, patch, window, *, signals=None, control=None):
        self.window = window
        self.signals = signals or {
            name: threading.Event() for name in (
                "activation", "prepared", "entered", "release", "cleanup_entered",
                "cleanup_release", "cleanup_finished", "reply_entered", "reply_release",
            )
        }
        self.events, self.preparations, self.hosts, self.outcome = [], [], [], {}
        self.root = root
        pack = root / "pack"
        _write_manifest(pack)
        support = root / "support"
        support.mkdir()
        output = support / "host-output"
        output.mkdir()
        boot = support / "boot-manifest.json"
        boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
        boot_hash = "sha256:" + load_boot_manifest_hash(boot, support_root=support)
        self.ready = support / "ready.json"
        credential = support / "test-worker-credential"
        credential.write_text("synthetic-fix11-worker")
        generic_host.platform.owner_only(credential)
        owner = {
            "pid": os.getpid(), "uid": getattr(os, "getuid", lambda: 0)(),
            "birth_id": "fix11-relay", "runtime_instance_id": "epoch", "coordinator_epoch": "epoch",
        }
        launch = {
            "source_checkout": str(root), "pack_root": str(pack),
            "boot_manifest_path": str(boot), "boot_manifest_hash": boot_hash,
            "capability_matrix": None, "environment": {}, "max_concurrency": 1,
        }
        profile = {
            "execution_mode": "host_only_cpu", "profile_id": "fix11", "workspace_uuid": "realm",
            "realm_root": str(root / "realm"), "support_root": str(support), "machine_id": "machine",
            "host_executable": sys.executable,
            "host_artifact_digest": "sha256:" + hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
            "session_config_digest": generic_host._handoff_digest(launch), "profile_revision": "revision",
            "profile_digest": "sha256:" + "1" * 64, "release_digest": "sha256:" + "2" * 64,
            "host_protocol": {"version": generic_host.CURRENT_ROLES_VERSION,
                              "module": "astrid.core.execution.generic_host", "source_closure_digest": "3" * 64},
            "host_launch": launch,
        }
        native = window in {"native_attachment", "native_reply"}
        if native:
            profile = _local_custody_request(root)["profile"]
        self.request = {
            "version": generic_host._LOCAL_PREPARATION_VERSION, "command": "prepare_local_execution",
            "operation_id": "op", "channel_id": "ch", "owner_epoch": "epoch", "runtime_owner": owner,
            "profile": profile, "custody_scope": str(root / "custody"),
        }
        grant = {
            "version": generic_host._ACTIVATION_VERSION, "operation_id": "op", "channel_id": "ch",
            "activation_id": "activation", "executor_incarnation": "incarnation",
            "evidence_digest": "sha256:" + "4" * 64,
            "host": {"pid": os.getpid(), "birth_id": generic_host.process_birth_identity()},
            "acceptance_mode": generic_host._ACTIVATION_RECEIPT_MODE,
        }
        refs = {} if native else {"host": {"target": grant["host"]}}
        self.abort = {
            "version": self.request["version"], "command": "abort_local_execution",
            "operation_id": "op", "channel_id": "ch", "owner_epoch": "epoch",
            "profile_digest": profile["profile_digest"],
            "profile_binding_digest": generic_host._handoff_digest(profile),
            "custody_scope": self.request["custody_scope"], "custody_capabilities": refs,
        }
        probe = self

        class Runtime(FakeRuntime):
            def _authenticate_worker(self, executor_id):
                return {"executor_id": executor_id}

            def register_executor(self, executor_id, **payload):
                probe.gate("inflight_registration")
                probe.events.append("register")
                return super().register_executor(executor_id, **payload)

            def claim_next(self, **payload):
                probe.gate("inflight_claim")
                probe.events.append("claim")
                if window in {"inflight_attempt", "inflight_settlement"}:
                    return {"task_id": "task", "lease_id": "lease", "attempt_id": "attempt",
                            "fence": 1, "capability_id": "test.echo", "runtime_epoch": 1}
                return super().claim_next(**payload)

            def task(self, task_id):
                probe.gate("inflight_attempt")
                raise generic_host.HostError("synthetic task read failure")

            def fail(self, task_id, lease_token, error, **kwargs):
                probe.gate("inflight_settlement")
                probe.events.append("settle")
                return super().fail(task_id, lease_token, error, **kwargs)

        class Graph:
            readiness_profile = {"verified_facts": {}}

            def __init__(self, *, request):
                self.request = request

            def prepare(self):
                return {"processes": {}, "engine_binding": {}, "session_config_digest": "measured",
                        "custody_capabilities": {}}

            def known_custody_capabilities(self):
                return {}

            def abort(self):
                probe.signals["cleanup_entered"].set()
                assert probe.signals["cleanup_release"].wait(20), "native cleanup was never released"
                probe.signals["cleanup_finished"].set()
                return {"engine": {"exit_code": 0}, "engine_listener": {"exit_code": 0}}

        self.runtime = Runtime()

        class RuntimeTransport:
            def __new__(cls, *args, **kwargs):
                return probe.runtime

        original_init = generic_host.LocalExecutionPreparation.__init__
        original_attach = generic_host.LocalExecutionPreparation.attach_claim_host
        original_host_init = generic_host.GenericPackHost.__init__
        original_register = generic_host.GenericPackHost.register
        original_publish = generic_host.GenericPackHost._publish_ready_registration
        original_claim = generic_host.GenericPackHost._handoff_enter_claim_rpc
        original_write = generic_host._write_ready_marker
        original_send = generic_host._send_activation_frame

        def capture_preparation(preparation, **kwargs):
            original_init(preparation, **kwargs, **({"graph_factory": Graph} if native else {}))
            probe.preparations.append(preparation)

        def accepted_activation(*args, **kwargs):
            probe.signals["activation"].set()
            assert probe.signals["prepared"].wait(20), "preparation never completed"
            return grant

        def capture_host(host, **kwargs):
            original_host_init(host, **kwargs)
            probe.hosts.append(host)

        def attach(preparation, host):
            if native:
                probe.gate(window)
            original_attach(preparation, host)
            probe.gate("attach")

        def register(host, **kwargs):
            probe.gate("registration")
            return original_register(host, **kwargs)

        def publish(host, registration):
            if host._ready_marker_path is not None:
                probe.gate("ready")
            return original_publish(host, registration)

        def claim(host):
            probe.gate("first_poll")
            return original_claim(host)

        def write(path, payload):
            if path == probe.ready and payload.get("status") == "ready":
                probe.gate("inflight_ready")
            return original_write(path, payload)

        def send(channel, frame):
            if window == "native_reply" and frame.get("status") == "cleaned":
                probe.signals["reply_entered"].set()
                assert probe.signals["reply_release"].wait(20), "cleanup reply was never released"
            return original_send(channel, frame)

        patch.setattr(generic_host.LocalExecutionPreparation, "__init__", capture_preparation)
        patch.setattr(generic_host.LocalExecutionPreparation, "attach_claim_host", attach)
        patch.setattr(generic_host.GenericPackHost, "__init__", capture_host)
        patch.setattr(generic_host.GenericPackHost, "register", register)
        patch.setattr(generic_host.GenericPackHost, "_publish_ready_registration", publish)
        patch.setattr(generic_host.GenericPackHost, "_handoff_enter_claim_rpc", claim)
        patch.setattr(generic_host, "_write_ready_marker", write)
        patch.setattr(generic_host, "_send_activation_frame", send)
        patch.setattr(generic_host, "_await_worker_activation", accepted_activation)
        patch.setattr(generic_host, "RuntimeProtocolClient", RuntimeTransport)
        patch.setattr(custody_broker.AuthenticatedCleanupActor, "private_peer",
                      staticmethod(lambda peer: SimpleNamespace(verify=lambda: owner)))

        def verify_reference(authority, reference, *, expected_actor, owner_epoch):
            assert expected_actor == owner and owner_epoch == "epoch"
            assert reference["target"] == grant["host"]

        patch.setattr(custody_broker.RoleCustodyAuthority, "verify_reference", verify_reference)
        patch.setattr(generic_host, "source_checkout_closure_digest", lambda source: "3" * 64)
        patch.setattr(generic_host, "_startup_identity_attestation",
                      lambda **kwargs: {"source": {"closure_digest": "3" * 64}})
        patch.setattr(generic_host, "_compose_cli_boot_manifest", lambda *args: (boot, boot_hash))
        patch.setattr(generic_host, "model_root_binding_from_profile",
                      lambda *a, **k: SimpleNamespace(path=root / "models", as_dict=lambda: {}))
        if not native:
            patch.setattr(generic_host.platform, "install_stop_handlers", lambda handler: None)
        patch.setattr(generic_host.faulthandler, "register", lambda *args, **kwargs: None)
        patch.setattr(generic_host, "configure_process_control", lambda root: None)
        patch.setattr(generic_host, "start_stdio_rotator", lambda root: None)
        patch.setattr(generic_host._task_scope, "_journal", None)
        patch.setattr(generic_host, "_STARTUP_OBSERVATION_ENABLED", False)
        patch.setenv("ASTRID_OUTPUT_ROOT", str(output))
        patch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(support / "data"))
        for name in ("ASTRID_EXECUTION_TARGET_JSON", "ASTRID_PACK_HOST_SERVICE_MODE",
                     "ASTRID_HOST_READINESS_PROFILE_PATH", "ASTRID_REMOTION_VALIDATION_LAUNCH"):
            patch.delenv(name, raising=False)
        assert generic_host._runtime_external_sources is None
        if control is None:
            self.relay, control = socket.socketpair()
            self.relay.settimeout(20)
        self.activation_peer, self.activation = socket.socketpair()
        patch.setattr(sys, "argv", [
            "host", "run", *([] if native else ["--once"]),
            "--pack-root", str(pack), "--support-root", str(support),
            "--ready-file", str(self.ready), "--runtime-endpoint", "http://127.0.0.1:1",
            "--credential-file", str(credential), "--host-control-fd", str(control.detach()),
            "--activation-fd", str(self.activation.fileno()), "--activation-operation-id", "op",
            "--activation-channel-id", "ch",
        ])

    def gate(self, window):
        if self.window == window:
            self.signals["entered"].set()
            assert self.signals["release"].wait(20), f"{window} was never released"

    def run(self):
        try:
            self.outcome["result"] = generic_host._cli()
        except BaseException as exc:  # noqa: BLE001 - return CLI/parser exits to the probe owner
            self.outcome["error"] = exc

    def acknowledge(self):
        generic_host._send_activation_frame(self.relay, self.abort)
        reply = generic_host._read_activation_frame(self.relay)
        assert reply["status"] == "cleaned", reply
        self.events.append("cleanup_ack")
        return reply


@contextmanager
def _running_cli(root, patch, window):
    probe = _CliProbe(root, patch, window)
    thread = threading.Thread(target=probe.run, name="fix11-cli")
    probe.thread = thread
    thread.start()
    try:
        assert probe.signals["activation"].wait(15), probe.outcome
        generic_host._send_activation_frame(probe.relay, probe.request)
        assert generic_host._read_activation_frame(probe.relay)["status"] == "prepared"
        probe.signals["prepared"].set()
        assert probe.signals["entered"].wait(15), probe.outcome
        yield probe
    finally:
        for signal in probe.signals.values():
            signal.set()
        thread.join(timeout=15)
        assert not thread.is_alive(), probe.outcome
        probe.relay.close()
        probe.activation_peer.close()
        probe.activation.close()
        for preparation in probe.preparations:
            preparation.control_thread.join(timeout=5)
            assert not preparation.control_thread.is_alive()
            if preparation._handoff_lock_fd is not None:
                os.close(preparation._handoff_lock_fd)
                preparation._handoff_lock_fd = None
        for host in probe.hosts:
            host.shutdown()


@pytest.mark.parametrize("window", ["attach", "registration", "ready", "first_poll"])
def test_cleanup_ack_fences_each_cli_activation_window(tmp_path, monkeypatch, window):
    with _running_cli(tmp_path, monkeypatch, window) as probe:
        probe.acknowledge()
        probe.signals["release"].set()
        probe.thread.join(timeout=15)
        assert not probe.thread.is_alive(), probe.outcome
        assert probe.events[probe.events.index("cleanup_ack") + 1:] == [], probe.events
        assert not probe.ready.exists(), probe.ready.read_text() if probe.ready.exists() else None


def test_no_cleanup_ack_preserves_normal_cli_activation(tmp_path, monkeypatch):
    with _running_cli(tmp_path, monkeypatch, "attach") as probe:
        probe.signals["release"].set()
        probe.thread.join(timeout=15)
        assert not probe.thread.is_alive(), probe.outcome
        assert probe.outcome == {"result": 0}
        assert probe.events == ["register", "claim"]
        assert json.loads(probe.ready.read_text())["status"] == "ready"


@pytest.mark.parametrize("window", [
    "inflight_registration", "inflight_ready", "inflight_claim", "inflight_attempt", "inflight_settlement",
])
def test_cleanup_ack_waits_for_admitted_work(tmp_path, monkeypatch, window):
    with _running_cli(tmp_path, monkeypatch, window) as probe:
        observed = {}

        def abort():
            observed["reply"] = probe.acknowledge()

        cleanup = threading.Thread(target=abort, name="fix11-relay")
        cleanup.start()
        try:
            assert probe.hosts[0]._shutdown.wait(5), "cleanup never reached the real host"
            cleanup.join(timeout=0.2)
            assert cleanup.is_alive(), "cleanup ACK overtook admitted work"
        finally:
            probe.signals["release"].set()
            cleanup.join(timeout=15)
        assert not cleanup.is_alive()
        assert observed["reply"]["status"] == "cleaned"
        probe.thread.join(timeout=15)
        assert not probe.thread.is_alive(), probe.outcome
        assert probe.events[-1] == "cleanup_ack", probe.events
        assert not probe.ready.exists()
        if window in {"inflight_attempt", "inflight_settlement"}:
            assert probe.events.index("settle") < probe.events.index("cleanup_ack")
            assert len(probe.runtime.failures) == 1


def _native_cli_child(root, control, metadata, signals, window):
    """Actual process exit exercises the daemon control-thread lifetime."""
    with pytest.MonkeyPatch.context() as patch:
        probe = _CliProbe(Path(root), patch, window, signals=signals, control=control)
        metadata.send({"request": probe.request, "abort": probe.abort})
        metadata.close()
        probe.run()
        if "error" in probe.outcome:
            print(f"native CLI exit: {probe.outcome['error']!r}", file=sys.stderr, flush=True)
        raise SystemExit(1 if "error" in probe.outcome else probe.outcome["result"])


class _FileSignal:
    """Monotonic fixture IPC remains readable after abrupt daemon-thread exit."""

    def __init__(self, path):
        self.path = path

    def set(self):
        self.path.touch()

    def is_set(self):
        return self.path.exists()

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.is_set():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True


@pytest.mark.parametrize("window", ["native_attachment", "native_reply"])
def test_native_cleanup_retains_cli_owner_and_reply(tmp_path, window):
    context = multiprocessing.get_context("spawn")
    signal_root = tmp_path / "signals"
    signal_root.mkdir()
    signals = {name: _FileSignal(signal_root / name) for name in (
        "activation", "prepared", "entered", "release", "cleanup_entered",
        "cleanup_release", "cleanup_finished", "reply_entered", "reply_release",
    )}
    relay, control = socket.socketpair()
    relay.settimeout(20)
    received, sent = context.Pipe(duplex=False)
    process = context.Process(target=_native_cli_child,
                              args=(str(tmp_path), control, sent, signals, window))
    process.start()
    control.close()
    sent.close()
    try:
        assert received.poll(15), "native CLI did not start"
        metadata = received.recv()
        assert signals["activation"].wait(15)
        generic_host._send_activation_frame(relay, metadata["request"])
        prepared = generic_host._read_activation_frame(relay)
        assert prepared["status"] == "prepared", prepared
        signals["prepared"].set()
        assert signals["entered"].wait(15), "native CLI never reached attachment"
        generic_host._send_activation_frame(relay, metadata["abort"])
        assert signals["cleanup_entered"].wait(10), "native cleanup did not start"
        signals["release"].set()
        if window == "native_attachment":
            # A real six-second hold: no shortened clock or mocked timeout.
            process.join(timeout=6)
            assert process.is_alive(), "CLI abandoned its native cleanup owner before release"
            assert not signals["cleanup_finished"].is_set()
            signals["cleanup_release"].set()
        else:
            signals["cleanup_release"].set()
            assert signals["reply_entered"].wait(10)
            process.join(timeout=0.5)
            assert process.is_alive(), "CLI exited before cleanup reply delivery"
            signals["reply_release"].set()
        reply = generic_host._read_activation_frame(relay)
        assert reply["status"] == "cleaned", reply
        assert set(reply["cleanup"]) == {"engine", "engine_listener"}
        assert signals["cleanup_finished"].is_set()
        # The unfixed base is a normal persistent worker. Stop it only after
        # the peer has observed its positive reply, through its real handler.
        process.join(timeout=1)
        if process.is_alive():
            process.terminate()
        process.join(timeout=15)
        assert not process.is_alive(), "CLI did not exit after completed cleanup and reply"
    finally:
        for signal in signals.values():
            signal.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        relay.close()
        received.close()
