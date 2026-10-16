"""Selected hosted startup fixtures; native identity and acceptor are doubled."""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_storage_preparation_transport import (
    REAL_VALIDATE_BINDING,
    exchange,
)
from test_storage_preparation_transport import (
    admitted as admitted,
)
from test_storage_preparation_transport import (
    world as world,
)

from astrid.core.execution import custody_broker as custody
from astrid.core.execution import generic_host as hosts
from astrid.core.execution import hosted_contract
from astrid.core.execution import storage_preparation_startup as startup
from astrid.core.execution import storage_preparation_transport as transport

REAL_ISSOCK = stat.S_ISSOCK


@pytest.fixture
def selected(request, monkeypatch):
    w = request.getfixturevalue("admitted")
    host = w.host
    host._registered_state = {w.record.id: {"capability_digest": w.record.capability_digest,
        "source_digest": w.record.source_digest, "dependency_digest": w.record.dependency_digest}}
    host.runtime_state = {"runtime_epoch": 1, "runtime_instance_id": w.config.runtime_instance_id,
        "coordinator_epoch": w.config.runtime_instance_id, "runtime_session_id": w.config.runtime_session_id}
    host._registered_runtime_state = {**host.runtime_state, "source_epoch": host.source_epoch}
    preparation = hosts.LocalExecutionPreparation(operation_id="operation", channel_id="channel")
    launch = {"pack_root": str(host.pack_roots[0]), "boot_manifest_path": str(host.boot_manifest_path),
              "boot_manifest_hash": host.boot_manifest_hash}
    request = {"operation_id": "operation", "channel_id": "channel", "owner_epoch": w.config.runtime_instance_id,
        "custody_scope": str(w.config.support_root), "profile": {"execution_mode": "host_only_cpu",
            "host_executable": sys.executable, "support_root": str(w.config.support_root),
            "workspace_uuid": w.config.runtime_realm_id, "host_launch": launch},
        "runtime_owner": {**{k: w.runtime_identity[k] for k in ("pid", "uid", "birth_id")},
            "runtime_instance_id": w.config.runtime_instance_id, "coordinator_epoch": w.config.runtime_instance_id}}
    broker = SimpleNamespace(role="host", owner_epoch=request["owner_epoch"], run_id="run", custody_generation=1,
                             authority_scope_root=w.config.support_root)
    hosted = hosted_contract.HostedLaunch(broker, request)
    argv = [sys.executable, "-m", "astrid.core.execution.generic_host", "run",
        "--host-control-fd", str(hosted.children["control"].fileno()),
        "--activation-fd", str(hosted.children["activation"].fileno()),
        "--activation-operation-id", "operation", "--activation-channel-id", "channel"]
    with monkeypatch.context() as native_descriptors:
        native_descriptors.setattr(stat, "S_ISSOCK", REAL_ISSOCK)
        hosted.environment(argv)
    # Real HostedLaunch descriptor/launch digests and verify_preparation logic;
    # only the external custody journal/native actor are synthetic.
    authority_calls = []
    class Authority:
        def __init__(self, scope, role):
            assert scope == w.config.support_root and role == "host"

        def reference(self):
            return {"generation": 1, "target": {"pid": os.getpid()}}

        def verify_reference(self, reference, *, expected_actor, owner_epoch):
            assert expected_actor == w.runtime_identity and owner_epoch == request["owner_epoch"]
            authority_calls.append(reference)
    monkeypatch.setattr(custody, "RoleCustodyAuthority", Authority)
    monkeypatch.setattr(startup, "validate_host_binding", REAL_VALIDATE_BINDING)
    monkeypatch.setattr(transport, "validate_host_binding", REAL_VALIDATE_BINDING)
    preparation._hosted_binding = hosted.value
    preparation._request = request
    observed_actor = custody.AuthenticatedCleanupActor()
    pinned = dict(observed_actor.verify())
    class RetainedActor:
        def verify(self):
            observed = observed_actor.verify()
            if observed != pinned:
                raise custody.CustodyError("retained Runtime incarnation changed")
            return observed
    preparation._retained_relay_actor = RetainedActor()
    preparation._claim_host = host
    preparation.result = {"status": "prepared"}
    grant = {"version": hosts._ACTIVATION_VERSION, "operation_id": "operation", "channel_id": "channel",
        "executor_incarnation": w.config.host_instance_id, "evidence_digest": "sha256:" + "c" * 64,
        "host": {"pid": w.config.host_peer.pid, "birth_id": w.config.host_peer.birth_id},
        "acceptance_mode": hosts._ACTIVATION_RECEIPT_MODE, "activation_id": "activation"}
    preparation.retain_activation_grant(grant)
    w.preparation, w.hosted, w.authority_calls = preparation, hosted, authority_calls
    try:
        yield w
    finally:
        hosted.close()


def test_exact_config_is_derived_from_selected_host_activation(selected, monkeypatch):
    w = selected
    for key in ("ASTRID_STORAGE_PREPARATION_PID", "ASTRID_STORAGE_PREPARATION_SOCKET", "ASTRID_EXECUTOR_ID"):
        monkeypatch.setenv(key, "untrusted")
    config = startup.configuration_from_activation(w.preparation)
    assert config == w.config
    assert w.authority_calls
    assert asdict(config.runtime_peer) == w.runtime_identity
    assert not w.calls  # Discovery/configuration has not imported or run producer code.


def test_selected_start_is_single_and_shutdown_owns_listener(selected):
    w = selected
    listener = w.preparation.start_storage_preparation()
    assert listener.host is w.host and listener.is_ready()
    w.frame["context"] = w.config.context
    w.frame["request"]["capability_digest"] = w.config.capability_digest
    # The fixture delegate returns the discovered, exact producer identity.
    original = w.host.prepare_storage_estimate
    def prepare(request):
        from dataclasses import replace
        return replace(original(request), source_digest=w.config.source_digest, estimator=w.config.estimator)
    w.host.prepare_storage_estimate = prepare
    assert exchange(w)["preparation"]["storage_estimate"] == {"scratch_bytes": 123, "output_bytes": 456}
    assert len(w.calls) == 1
    with pytest.raises(hosts.HostError, match="cannot start"):
        w.preparation.start_storage_preparation()
    w.host.shutdown()
    assert not listener._thread.is_alive()
    assert not w.config.socket_path.exists() and not w.config.readiness_path.exists()


@pytest.mark.parametrize("fault", ["no_hosted", "role", "generation", "profile", "relay", "owner_pid",
    "runtime_birth", "runtime_audit", "host_birth", "grant_host", "session", "instance", "unregistered",
    "source_epoch", "source_bytes", "boot", "roots", "aborted", "control_error", "legacy_activation",
    "ambiguous_capability"])
def test_untrusted_or_changed_startup_cannot_publish_or_dispatch(selected, fault):
    w = selected
    p, h = w.preparation, w.host
    if fault == "no_hosted":
        p._hosted_binding = None
    elif fault == "role":
        p._hosted_binding["role"] = "relay"
    elif fault == "generation":
        p._hosted_binding["generation"] += 1
    elif fault == "profile":
        p._request["profile"]["workspace_uuid"] = "foreign"
    elif fault == "relay":
        p._request["profile"]["execution_mode"] = "local_engine"
    elif fault == "owner_pid":
        p._request["runtime_owner"]["pid"] += 1
    elif fault == "runtime_birth":
        w.runtime_identity["birth_id"] = "foreign"
    elif fault == "runtime_audit":
        w.runtime_identity["audit_token_sha256"] = "sha256:" + "b" * 64
    elif fault == "host_birth":
        w.host_identity["birth_id"] = "foreign"
    elif fault == "grant_host":
        p._activation_grant["host"]["pid"] += 1
    elif fault == "session":
        h.runtime_state["runtime_session_id"] = "foreign"
    elif fault == "instance":
        h._registered_runtime_state["runtime_instance_id"] = "foreign"
    elif fault == "unregistered":
        h._registered_state.clear()
    elif fault == "source_epoch":
        h.source_epoch = "foreign"
    elif fault == "source_bytes":
        w.source.write_text("changed bytes\n")
    elif fault == "boot":
        h.boot_manifest_hash = "b" * 64
    elif fault == "roots":
        h.pack_roots = ()
    elif fault == "aborted":
        p._aborted = True
    elif fault == "control_error":
        p.control_error = RuntimeError("closed")
    elif fault == "legacy_activation":
        p._activation_grant.pop("acceptance_mode")
    elif fault == "ambiguous_capability":
        h.capabilities["other"] = w.record
    with pytest.raises((transport.PreparationTransportError, hosted_contract.HostedContractError, custody.CustodyError)):
        p.start_storage_preparation()
    assert not w.config.readiness_path.exists() and not w.config.socket_path.exists()
    assert h._storage_preparation_listener is None and not w.calls


def test_discovery_change_retires_endpoint_and_prevents_restart(selected):
    w = selected
    listener = w.preparation.start_storage_preparation()
    w.source.write_text("changed bytes\n")
    w.host.discover()
    assert not listener._thread.is_alive() and not w.config.readiness_path.exists()
    assert not w.config.socket_path.exists() and not w.calls
    with pytest.raises(transport.PreparationTransportError):
        w.preparation.start_storage_preparation()


@pytest.mark.parametrize("key,value,reason", [("runtime_session_id", "next-session", "session changed"),
    ("runtime_epoch", 2, "epoch changed")])
def test_runtime_session_change_retires_endpoint(selected, monkeypatch, key, value, reason):
    w = selected
    listener = w.preparation.start_storage_preparation()
    runtime = {**w.host.runtime_state, key: value}
    monkeypatch.setattr(w.host, "_runtime_compatibility", lambda: runtime)
    with pytest.raises(hosts.HostError, match=reason):
        w.host._register_impl()
    assert not listener._thread.is_alive()
    assert not w.config.readiness_path.exists() and not w.calls


@pytest.mark.parametrize("claim_failure", [False, True])
def test_cli_starts_after_discovery_and_registration_and_closes_on_exit(selected, monkeypatch, claim_failure):
    """Run the actual CLI sequence with launch/native/HTTP boundaries doubled."""
    w = selected
    p, h = w.preparation, w.host
    root = w.config.support_root
    output = root / "host-output"
    output.mkdir()
    credential = root / "worker-credential"
    credential.write_text("synthetic")
    credential.chmod(0o600)
    ready = root / "host-ready.json"
    events = []
    real_discover = h.discover
    def discover():
        events.append("discovery")
        return real_discover()
    def register():
        assert events == ["discovery"]
        assert h._storage_preparation_listener is None
        events.append("registration")
        return {"executor_id": h.executor_id, "capabilities": []}
    def claim_loop(**kwargs):
        assert events == ["discovery", "registration"]
        assert h._storage_preparation_listener.is_ready()
        assert ready.is_file()
        events.append("claims")
        if claim_failure:
            raise RuntimeError("synthetic claim failure")
        return []
    monkeypatch.setattr(h, "discover", discover)
    monkeypatch.setattr(h, "register", register)
    monkeypatch.setattr(h, "run", claim_loop)
    monkeypatch.setattr(hosts, "GenericPackHost", lambda **kwargs: h)
    monkeypatch.setattr(hosts, "LocalExecutionPreparation", lambda **kwargs: p)
    monkeypatch.setattr(p, "serve_control", lambda fd: None)
    monkeypatch.setattr(p, "attach_claim_host", lambda owner: None if owner is h else pytest.fail("foreign owner"))
    monkeypatch.setattr(hosted_contract, "consume", lambda *args: p._hosted_binding)
    monkeypatch.setattr(hosts, "_await_worker_activation", lambda *args, **kwargs: p._activation_grant)
    monkeypatch.setattr(hosts, "_await_enabled_runtime_credential", lambda *args, **kwargs: None)
    monkeypatch.setattr(hosts, "RuntimeProtocolClient", lambda *args: object())
    monkeypatch.setattr(hosts, "_compose_cli_boot_manifest", lambda *args: (h.boot_manifest_path, h.boot_manifest_hash))
    monkeypatch.setattr(hosts, "_startup_identity_attestation", lambda **kwargs: {"source": {"closure_digest": "synthetic"}})
    monkeypatch.setattr(hosts, "_install_host_vibecomfy_import_root", lambda: None)
    monkeypatch.setattr(hosts, "start_stdio_rotator", lambda *args: None)
    monkeypatch.setattr(hosts.signal, "signal", lambda *args: None)
    monkeypatch.setattr(hosts.faulthandler, "register", lambda *args, **kwargs: None)
    monkeypatch.setenv("ASTRID_OUTPUT_ROOT", str(output))
    monkeypatch.setattr(sys, "argv", ["astrid-generic-host", "run", "--register", "--once",
        "--pack-root", str(h.pack_roots[0]), "--support-root", str(root), "--executor-id", h.executor_id,
        "--runtime-endpoint", "http://127.0.0.1:1", "--runtime-instance-id", w.config.runtime_instance_id,
        "--credential-file", str(credential), "--ready-file", str(ready),
        "--activation-operation-id", p.operation_id, "--activation-channel-id", p.channel_id,
        "--activation-fd", str(w.hosted.children["activation"].fileno()),
        "--host-control-fd", str(w.hosted.children["control"].fileno())])
    if claim_failure:
        with pytest.raises(RuntimeError, match="synthetic claim failure"):
            hosts._cli()
    else:
        assert hosts._cli() == 0
    assert events == ["discovery", "registration", "claims"]
    assert not h._storage_preparation_listener._thread.is_alive()
    assert not w.config.readiness_path.exists() and not w.config.socket_path.exists()
    assert not w.calls


def test_claim_slot_never_copies_the_listener(selected):
    w = selected
    w.preparation.start_storage_preparation()
    w.host.client = SimpleNamespace(fork_for_host_slot=lambda: None)
    slot = w.host._new_claim_slot()
    assert slot is not w.host
    assert slot._storage_preparation_listener is None
    assert slot._storage_preparation_lock is not w.host._storage_preparation_lock
    slot.shutdown()
