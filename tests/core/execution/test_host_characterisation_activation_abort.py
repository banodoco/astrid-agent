"""HM2 R12/R14: an acknowledged control abort must fence CLI activation.

Preparation, control framing/dispatch, activation retention, host discovery,
registration and the claim loop are real. Peer/role verification, activation
acceptance and Runtime transport are boundary doubles, not native custody proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash
from astrid.core.execution import custody_broker, generic_host
from tests.test_generic_host import FakeRuntime, _write_manifest


def test_r12_r14_cleanup_ack_during_activation_prevents_later_readiness(tmp_path, monkeypatch):
    """The abort ACK cannot precede a new ready host or its first Runtime claim."""
    pack = tmp_path / "pack"
    _write_manifest(pack)
    support = tmp_path / "support"
    support.mkdir()
    output = support / "host-output"
    output.mkdir()
    boot = support / "boot-manifest.json"
    boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
    boot_hash = "sha256:" + load_boot_manifest_hash(boot, support_root=support)
    ready = support / "ready.json"
    credential = support / "test-worker-credential"
    credential.write_text("synthetic-hm2-worker")
    generic_host.platform.owner_only(credential)
    owner = {
        "pid": os.getpid(), "uid": getattr(os, "getuid", lambda: 0)(),
        "birth_id": "hm2-relay", "runtime_instance_id": "epoch", "coordinator_epoch": "epoch",
    }
    launch = {
        "source_checkout": str(tmp_path), "pack_root": str(pack),
        "boot_manifest_path": str(boot), "boot_manifest_hash": boot_hash,
        "capability_matrix": None, "environment": {}, "max_concurrency": 1,
    }
    profile = {
        "execution_mode": "host_only_cpu", "profile_id": "hm2", "workspace_uuid": "realm",
        "realm_root": str(tmp_path / "realm"), "support_root": str(support), "machine_id": "machine",
        "host_executable": sys.executable,
        "host_artifact_digest": "sha256:" + hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        "session_config_digest": generic_host._handoff_digest(launch), "profile_revision": "revision",
        "profile_digest": "sha256:" + "1" * 64, "release_digest": "sha256:" + "2" * 64,
        "host_protocol": {
            "version": generic_host.CURRENT_ROLES_VERSION,
            "module": "astrid.core.execution.generic_host", "source_closure_digest": "3" * 64,
        },
        "host_launch": launch,
    }
    preparation_request = {
        "version": generic_host._LOCAL_PREPARATION_VERSION, "command": "prepare_local_execution",
        "operation_id": "op", "channel_id": "ch", "owner_epoch": "epoch", "runtime_owner": owner,
        "profile": profile, "custody_scope": str(tmp_path / "custody"),
    }
    grant = {
        "version": generic_host._ACTIVATION_VERSION, "operation_id": "op", "channel_id": "ch",
        "activation_id": "activation", "executor_incarnation": "incarnation",
        "evidence_digest": "sha256:" + "4" * 64,
        "host": {"pid": os.getpid(), "birth_id": generic_host.process_birth_identity()},
        "acceptance_mode": generic_host._ACTIVATION_RECEIPT_MODE,
    }
    events, preparations, hosts, outcome = [], [], [], {}
    activation_waiting, prepared = threading.Event(), threading.Event()
    retention_entered, release_retention = threading.Event(), threading.Event()

    class Runtime(FakeRuntime):
        def _authenticate_worker(self, executor_id):
            return {"executor_id": executor_id}

        def register_executor(self, executor_id, **payload):
            events.append("register")
            return super().register_executor(executor_id, **payload)

        def claim_next(self, **payload):
            events.append("claim")
            return super().claim_next(**payload)

    runtime = Runtime()

    class RuntimeTransport:
        def __new__(cls, *args, **kwargs):
            return runtime

    original_init = generic_host.LocalExecutionPreparation.__init__
    original_retain = generic_host.LocalExecutionPreparation.retain_activation_grant
    original_host_init = generic_host.GenericPackHost.__init__

    def capture_preparation(self, **kwargs):
        original_init(self, **kwargs)
        preparations.append(self)

    def accepted_activation(*args, **kwargs):
        activation_waiting.set()
        assert prepared.wait(10), "control preparation never completed"
        assert preparations[0].result["status"] == "prepared"
        return grant

    def gated_retention(self, accepted):
        retention_entered.set()
        assert release_retention.wait(10), "abort ACK was never released to activation"
        return original_retain(self, accepted)

    def capture_host(self, **kwargs):
        original_host_init(self, **kwargs)
        hosts.append(self)

    def verify_reference(self, reference, *, expected_actor, owner_epoch):
        assert expected_actor == owner and owner_epoch == "epoch"
        assert reference["target"] == grant["host"]

    monkeypatch.setattr(generic_host.LocalExecutionPreparation, "__init__", capture_preparation)
    monkeypatch.setattr(generic_host.LocalExecutionPreparation, "retain_activation_grant", gated_retention)
    monkeypatch.setattr(generic_host, "_await_worker_activation", accepted_activation)
    monkeypatch.setattr(generic_host.GenericPackHost, "__init__", capture_host)
    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", RuntimeTransport)
    monkeypatch.setattr(custody_broker.AuthenticatedCleanupActor, "private_peer",
                        staticmethod(lambda peer: SimpleNamespace(verify=lambda: owner)))
    monkeypatch.setattr(custody_broker.RoleCustodyAuthority, "verify_reference", verify_reference)
    monkeypatch.setattr(generic_host, "source_checkout_closure_digest", lambda source: "3" * 64)
    monkeypatch.setattr(generic_host, "_startup_identity_attestation",
                        lambda **kwargs: {"source": {"closure_digest": "3" * 64}})
    monkeypatch.setattr(generic_host, "_compose_cli_boot_manifest", lambda *args: (boot, boot_hash))
    monkeypatch.setattr(generic_host.platform, "install_stop_handlers", lambda handler: None)
    monkeypatch.setattr(generic_host.faulthandler, "register", lambda *args, **kwargs: None)
    monkeypatch.setattr(generic_host, "configure_process_control", lambda root: None)
    monkeypatch.setattr(generic_host, "start_stdio_rotator", lambda root: None)
    monkeypatch.setattr(generic_host._task_scope, "_journal", None)
    monkeypatch.setattr(generic_host, "_STARTUP_OBSERVATION_ENABLED", False)
    monkeypatch.setenv("ASTRID_OUTPUT_ROOT", str(output))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(support / "data"))
    for name in ("ASTRID_EXECUTION_TARGET_JSON", "ASTRID_PACK_HOST_SERVICE_MODE",
                 "ASTRID_HOST_READINESS_PROFILE_PATH", "ASTRID_REMOTION_VALIDATION_LAUNCH"):
        monkeypatch.delenv(name, raising=False)
    assert generic_host._runtime_external_sources is None

    relay, control = socket.socketpair()
    activation_peer, activation = socket.socketpair()
    relay.settimeout(10)
    monkeypatch.setattr(sys, "argv", [
        "host", "run", "--once", "--pack-root", str(pack), "--support-root", str(support),
        "--ready-file", str(ready), "--runtime-endpoint", "http://127.0.0.1:1",
        "--credential-file", str(credential), "--host-control-fd", str(control.detach()),
        "--activation-fd", str(activation.fileno()), "--activation-operation-id", "op",
        "--activation-channel-id", "ch",
    ])

    def start_cli():
        try:
            outcome["result"] = generic_host._cli()
        except BaseException as exc:  # noqa: BLE001 - return parser exits to the owning test thread
            outcome["error"] = exc

    thread = threading.Thread(target=start_cli, name="hm2-cli-activation")
    thread.start()
    try:
        assert activation_waiting.wait(10)
        generic_host._send_activation_frame(relay, preparation_request)
        response = generic_host._read_activation_frame(relay)
        assert response["status"] == "prepared", response
        prepared.set()
        assert retention_entered.wait(10), "CLI never passed its initial activation check"
        preparation = preparations[0]
        assert preparation._claim_host is None
        abort = {
            "version": generic_host._LOCAL_PREPARATION_VERSION, "command": "abort_local_execution",
            "operation_id": "op", "channel_id": "ch", "owner_epoch": "epoch",
            "profile_digest": profile["profile_digest"],
            "profile_binding_digest": generic_host._handoff_digest(profile),
            "custody_scope": preparation_request["custody_scope"],
            "custody_capabilities": {"host": {"target": grant["host"]}},
        }
        generic_host._send_activation_frame(relay, abort)
        acknowledged = generic_host._read_activation_frame(relay)
        assert acknowledged == {
            **{key: value for key, value in abort.items() if key != "command"},
            "status": "cleaned", "cleanup": {"host_children": "absent"},
        }
        assert preparation._aborted is True
        assert preparation._claim_host is None and hosts == []
        events.append("cleanup_ack")
        release_retention.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert "error" not in outcome or isinstance(
            outcome["error"], (generic_host.HostError, SystemExit),
        ), outcome
        marker = json.loads(ready.read_text()) if ready.exists() else None
        observed = {
            "events": events, "registrations": len(runtime.registrations),
            "readiness": marker.get("status") if marker else None,
        }
        assert observed == {"events": ["cleanup_ack"], "registrations": 0, "readiness": None}, outcome
    finally:
        prepared.set()
        release_retention.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        relay.close()
        activation_peer.close()
        activation.close()
        for preparation in preparations:
            preparation.control_thread.join(timeout=5)
            assert not preparation.control_thread.is_alive()
            if preparation._handoff_lock_fd is not None:
                os.close(preparation._handoff_lock_fd)
                preparation._handoff_lock_fd = None
        for host in hosts:
            host.shutdown()
