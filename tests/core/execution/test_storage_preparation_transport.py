"""Transport fixtures only: no actual Runtime process or peer authentication.

Socketpairs exercise real byte I/O. The acceptor/path inode is synthetic to keep
all filesystem writes in the authorized evidence scratch, whose absolute path
exceeds Darwin's UDS path limit. Native identity helpers are explicitly doubled.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import socket
import stat
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from astrid.core.execution import custody_broker as custody
from astrid.core.execution import generic_host as hosts
from astrid.core.execution import storage_preparation_transport as transport
from astrid.core.execution.guards import ExecutionGuardPolicy
from astrid.core.execution.storage_estimator import StoragePreparation

D = "sha256:" + "a" * 64
REAL_VALIDATE_BINDING = transport.validate_host_binding


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "support"
    root.mkdir(mode=0o700)
    host_peer = transport.ProcessIdentity(os.getpid(), os.getuid(), "host-birth", D, 10)
    runtime_peer = replace(host_peer, pid=os.getpid() + 1, birth_id="runtime-birth", audit_token_pidversion=11)
    config = transport.StoragePreparationConfig(root, "host-instance", host_peer, runtime_peer,
        "a" * 64, "realm", "runtime-instance", "runtime-session", "executor", "source-epoch",
        "pack.action", D, D, D, "estimator", 1, D, 7, D, D)
    host = hosts.GenericPackHost(pack_roots=[], executor_id="executor",
                                execution_policy=ExecutionGuardPolicy(deadline_seconds=0.3))
    w = SimpleNamespace(config=config, host=host, calls=[], accepts=queue.Queue(),
                        host_identity=asdict(host_peer), runtime_identity=asdict(runtime_peer),
                        peer_identity=asdict(runtime_peer), source_valid=True)

    class Actor:
        def __init__(self, *args):
            self.kind = "runtime"

        @classmethod
        def current(cls):
            actor = cls()
            actor.kind = "host"
            return actor

        @classmethod
        def private_peer(cls, channel):
            actor = cls()
            actor.kind = "peer"
            return actor

        def verify(self):
            value = getattr(w, self.kind + "_identity")
            if value is None:
                raise custody.CustodyError("unavailable native observation")
            return copy.deepcopy(value)

    monkeypatch.setattr(custody, "AuthenticatedCleanupActor", Actor)
    def validate(host, config):
        if not w.source_valid:
            raise transport.PreparationTransportError("source_mismatch")
    monkeypatch.setattr(transport, "validate_host_binding", validate)

    # A fixture acceptor, not a second service/process or production path alias.
    real_socket = socket.socket
    class Acceptor:
        def bind(self, path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)

        def listen(self, backlog):
            pass

        def settimeout(self, timeout):
            pass

        def accept(self):
            try:
                return w.accepts.get(timeout=0.02), None
            except queue.Empty:
                raise socket.timeout from None

        def close(self):
            pass
    def factory(*args, **kwargs):
        return Acceptor() if len(args) == 2 and not kwargs else real_socket(*args, **kwargs)
    monkeypatch.setattr(socket, "socket", factory)
    monkeypatch.setattr(transport.stat, "S_ISSOCK", stat.S_ISREG)
    monkeypatch.setattr(custody, "DARWIN_UNIX_SOCKET_PATH_MAX_BYTES", 4096)
    def prepare(request):
        w.calls.append(copy.deepcopy(request))
        return StoragePreparation(copy.deepcopy(request), D, D, config.estimator,
                                  {"scratch_bytes": 123, "output_bytes": 456})
    host.prepare_storage_estimate = prepare
    w.frame = {"protocol": transport.PROTOCOL, "command": transport.COMMAND,
        "request_id": "request-1", "context": config.context,
        "request": {"capability_id": config.capability_id, "capability_digest": D,
            "schema_version": 1, "input_facts": [], "spec": {
                "params": {"raw": [None, True, {"雪": "line\nvalue"}]},
                "output_policy": {"keep": "all"}, "extra": [1, 2]}}}
    yield w
    host.shutdown()


def exchange(w, frame=None, raw=None):
    client, server = socket.socketpair()
    client.settimeout(1)
    w.accepts.put(server)
    try:
        try:
            client.sendall(raw if raw is not None else transport._encoded(frame or w.frame))
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError as exc:
            if exc.errno != 57:  # Darwin reports a peer already closed.
                raise
        chunks = []
        while True:
            try:
                chunk = client.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return json.loads(b"".join(chunks)) if chunks else None
    finally:
        client.close()


def test_roundtrip_exact_flat_payload_and_single_delegation(world):
    w = world
    listener = w.host.start_storage_preparation(w.config)
    reply = exchange(w)
    assert len(w.calls) == 1
    assert w.calls[0].input_facts == ()
    assert reply == {"protocol": transport.PROTOCOL, "command": transport.COMMAND,
        "request_id": "request-1", "context": w.config.context,
        "preparation": StoragePreparation(w.calls[0], D, D, w.config.estimator,
            {"scratch_bytes": 123, "output_bytes": 456}).to_dict()}
    assert len(w.calls) == 1
    assert reply["preparation"]["spec"] == w.frame["request"]["spec"]
    assert reply["preparation"]["storage_estimate"] == {"scratch_bytes": 123, "output_bytes": 456}
    assert listener._thread.is_alive()
    assert listener.is_ready()
    with pytest.raises(hosts.HostError):
        w.host.start_storage_preparation(w.config)
    w.host.shutdown()
    assert not listener._thread.is_alive()
    assert not w.config.socket_path.exists() and not w.config.readiness_path.exists()
    with pytest.raises(hosts.HostError):
        w.host.start_storage_preparation(w.config)


@pytest.mark.parametrize("field,value", [("uid", 999), ("pid", 999), ("birth_id", "other"),
    ("audit_token_sha256", "sha256:" + "b" * 64), ("audit_token_pidversion", 999)])
def test_foreign_actual_peer_never_delegates(world, field, value):
    w = world
    w.host.start_storage_preparation(w.config)
    w.peer_identity[field] = value
    assert exchange(w) is None
    assert w.calls == []


@pytest.mark.parametrize("kind", ["host", "runtime"])
def test_unavailable_or_changed_startup_identity_fails_closed(world, kind):
    w = world
    setattr(w, kind + "_identity", None)
    with pytest.raises(custody.CustodyError):
        w.host.start_storage_preparation(w.config)
    assert not w.config.readiness_path.exists()


@pytest.mark.parametrize("fault", ["context", "protocol", "command", "extra", "facts", "inputs",
    "input_digests", "input_object_ids", "runtime_dependencies", "timeline_snapshot", "schema_bool", "schema_string", "body_extra", "capability"])
def test_request_refusals_before_estimator(world, fault):
    w = world
    w.host.start_storage_preparation(w.config)
    frame = copy.deepcopy(w.frame)
    expected = "invalid_request"
    if fault == "context":
        frame["context"]["source_epoch"] = "stale"
        expected = "context_mismatch"
    elif fault in {"protocol", "command"}:
        frame[fault] = "arbitrary"
        expected = "unsupported_operation"
    elif fault == "extra":
        frame["socket_path"] = "/untrusted"
    elif fault == "facts":
        frame["request"]["input_facts"] = [{"object_id": D, "size_bytes": 1}]
        expected = "input_facts_unavailable"
    elif fault in {"inputs", "input_digests", "input_object_ids", "runtime_dependencies", "timeline_snapshot"}:
        frame["request"]["spec"][fault] = [D]
        expected = "input_facts_unavailable"
    elif fault.startswith("schema"):
        frame["request"]["schema_version"] = True if fault == "schema_bool" else "1"
    elif fault == "body_extra":
        frame["request"]["source_digest"] = D
    else:
        frame["request"]["capability_id"] = "other.action"
    assert exchange(w, frame)["refusal"] == {"code": expected}
    assert not w.calls


@pytest.mark.parametrize("raw", [b'\xff\n', b'[]\n', b'{bad}\n', b'{"a":1,"a":2}\n',
    b'{"a":NaN}\n', b'{"a":Infinity}\n', b'{}\n{}\n', b'{"truncated":',
    b'x' * (transport.FRAME_LIMIT + 1)],
    ids=["utf8", "array", "json", "duplicates", "nan", "infinity", "two-frames", "truncated", "oversized"])
def test_malformed_truncated_oversized_multiple_frames_never_delegate(world, raw):
    w = world
    w.host.start_storage_preparation(w.config)
    assert exchange(w, raw=raw) is None
    assert not w.calls


@pytest.mark.parametrize("fault", ["missing", "stale", "symlink", "permissions", "socket_permissions", "root_permissions", "support_permissions"])
def test_readiness_and_endpoint_tamper_refuses(world, fault):
    w = world
    w.host.start_storage_preparation(w.config)
    p = w.config.readiness_path
    if fault == "missing":
        p.unlink()
    elif fault == "stale":
        p.write_text('{"stale":true}')
    elif fault == "symlink":
        p.unlink()
        p.symlink_to(w.config.support_root / "absent")
    elif fault == "permissions":
        p.chmod(0o644)
    elif fault == "socket_permissions":
        w.config.socket_path.chmod(0o644)
    elif fault == "support_permissions":
        w.config.support_root.chmod(0o755)
    else:
        p.parent.chmod(0o755)
    assert exchange(w)["refusal"]
    assert not w.calls


@pytest.mark.parametrize("fault", ["noncanonical", "symlink", "permissions", "missing", "stale_socket", "stale_record", "long_path"])
def test_bad_startup_endpoint_never_becomes_ready(world, fault, monkeypatch):
    w = world
    config = w.config
    if fault == "symlink":
        link = config.support_root.parent / "link"
        link.symlink_to(config.support_root, target_is_directory=True)
        config = replace(config, support_root=link)
    elif fault == "noncanonical":
        config = replace(config, support_root=config.support_root / ".." / "support")
    elif fault == "permissions":
        config.support_root.chmod(0o755)
    elif fault == "missing":
        config = replace(config, support_root=config.support_root / "absent")
    elif fault == "long_path":
        monkeypatch.setattr(custody, "DARWIN_UNIX_SOCKET_PATH_MAX_BYTES", 103)
    else:
        config.socket_path.parent.mkdir(mode=0o700)
        target = config.socket_path if fault == "stale_socket" else config.readiness_path
        target.write_bytes(b"preserve stale input")
        target.chmod(0o600)
    with pytest.raises((transport.PreparationTransportError, OSError)):
        w.host.start_storage_preparation(config)
    if fault in {"stale_socket", "stale_record"}:
        assert target.read_bytes() == b"preserve stale input"
    assert w.host._storage_preparation_listener is None


@pytest.mark.parametrize("fault", ["source", "runtime_dead", "host_birth", "estimator_error", "late", "reply_spec", "reply_estimator"])
def test_inflight_or_preflight_failure_never_returns_quote(world, fault):
    w = world
    w.host.start_storage_preparation(w.config)
    original = w.host.prepare_storage_estimate
    def prepare(request):
        result = original(request)
        if fault == "source":
            w.source_valid = False
        elif fault == "runtime_dead":
            w.runtime_identity = None
        elif fault == "host_birth":
            w.host_identity["birth_id"] = "new"
        elif fault == "estimator_error":
            raise hosts.HostError("invalid estimator")
        elif fault == "late":
            time.sleep(0.35)
        elif fault == "reply_spec":
            result.request.spec["extra"] = ["mutated"]
        elif fault == "reply_estimator":
            result = replace(result, estimator={"id": "other"})
        return result
    w.host.prepare_storage_estimate = prepare
    reply = exchange(w)
    assert reply is None if fault == "late" else "refusal" in reply
    assert len(w.calls) == 1


def test_timeout_total_frame_deadline_and_host_close(world):
    w = world
    listener = w.host.start_storage_preparation(w.config)
    client, server = socket.socketpair()
    client.settimeout(1)
    w.accepts.put(server)
    client.sendall(b'{')
    assert client.recv(1) == b''
    client.close()
    assert not w.calls
    w.host.shutdown()
    assert not listener._thread.is_alive()


def test_one_connection_gets_only_one_response(world):
    w = world
    w.host.start_storage_preparation(w.config)
    assert exchange(w)["preparation"]
    second = copy.deepcopy(w.frame)
    second["request_id"] = "fresh-request"
    assert exchange(w, second)["request_id"] == "fresh-request"
    assert len(w.calls) == 2  # No cached quote or per-quote process.


@pytest.mark.parametrize("mutation", ["pid", "birth_id", "audit_token_pidversion", "audit_token_sha256"])
def test_identity_config_is_strict(world, mutation):
    with pytest.raises(transport.PreparationTransportError):
        replace(world.config.runtime_peer, **{mutation: None})


def test_close_preserves_replacement_record_inode(world):
    w = world
    w.host.start_storage_preparation(w.config)
    ready = w.config.readiness_path
    held = ready.with_suffix(".old")
    ready.rename(held)
    ready.write_text("foreign replacement")
    w.host.shutdown()
    assert ready.read_text() == "foreign replacement"


@pytest.fixture
def admitted(world):
    """Real discovery/source/estimator checks, without running producer code."""
    w = world
    root = w.config.support_root / "pack"
    actions = root / "actions"
    actions.mkdir(parents=True)
    source = actions / "estimate.py"
    source.write_text("raise AssertionError('discovery/readiness must not import estimator')\n")
    descriptor = {"schema_version": 1, "id": "estimator", "version": 1,
        "policy_version": "v1", "path": "actions/estimate.py", "function": "estimate"}
    declaration = {"kind": "generator", "description": "Synthetic preparation fixture",
        "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "pass"]}},
        "inputs": {"type": "object", "properties": {}, "additionalProperties": True},
        "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt",
                     "artifact_type": "text/plain"}],
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"], "storage_estimator": descriptor}}
    (root / "pack.yaml").write_text(json.dumps({"schema_version": 3, "id": "pack", "name": "Pack",
        "version": "1.0.0", "actions": {"action": declaration}}))
    w.host.pack_roots = (root,)
    w.host.discover()
    record = w.host.capabilities["pack.action"]
    w.host._registered_runtime_state = {**w.config.context["runtime"]}
    w.config = replace(w.config, source_epoch=w.host.source_epoch,
        executor_source_digest="sha256:" + hosts._canonical_digest({k: v.source_digest for k, v in w.host.capabilities.items()}),
        executor_dependency_digest="sha256:" + hosts._canonical_digest({k: v.dependency_digest for k, v in w.host.capabilities.items()}),
        capability_digest=record.capability_digest, source_digest="sha256:" + record.source_digest,
        dependency_digest="sha256:" + record.dependency_digest,
        estimator_implementation_digest=record.request_storage_estimate_contract["estimator"]["implementation_digest"])
    w.host.boot_manifest_path = w.config.support_root / "boot" / "manifest.json"
    w.host.boot_manifest_hash = w.config.boot_manifest_hash
    w.host.boot_manifest_provenance = lambda: {"kind": "astrid.boot_manifest", "sha256": w.config.boot_manifest_hash}
    w.source = source
    w.record = record
    return w


def test_real_admitted_source_and_descriptor_readiness_without_import(admitted):
    w = admitted
    assert not w.record.approval_reason, w.record.approval_reason
    assert not hosts._is_withdrawn(w.record), w.record.matrix
    checks = {"record_lookup": w.host.capabilities.get(w.config.capability_id) is w.record,
        "has_estimator": w.record.has_request_storage_estimator, "capability": w.record.capability_digest == w.config.capability_digest,
        "source": "sha256:" + w.record.source_digest == w.config.source_digest,
        "dependency": "sha256:" + w.record.dependency_digest == w.config.dependency_digest,
        "dependency_recomputed": hosts._dependency_digest(w.record.definition, w.host.capabilities) == w.record.dependency_digest,
        "definition": hosts._capability_digest(w.record.definition.to_dict()) == w.config.capability_digest}
    assert all(checks.values()), checks
    REAL_VALIDATE_BINDING(w.host, w.config)
    w.source.write_text("changed bytes\n")
    with pytest.raises(transport.PreparationTransportError, match="source_mismatch"):
        REAL_VALIDATE_BINDING(w.host, w.config)


@pytest.mark.parametrize("fault", ["epoch", "executor", "capability", "source", "dependency", "estimator",
    "version", "boot", "manifest", "withdrawn", "unsupported_backend"])
def test_real_binding_refuses_mismatched_or_missing_inputs(admitted, fault, monkeypatch):
    w = admitted
    config = w.config
    if fault == "epoch":
        config = replace(config, source_epoch="stale")
    elif fault == "executor":
        config = replace(config, executor_id="foreign")
    elif fault in {"capability", "source", "dependency"}:
        config = replace(config, **{fault + "_digest": "sha256:" + "b" * 64})
    elif fault == "estimator":
        config = replace(config, estimator_id="other")
    elif fault == "version":
        config = replace(config, estimator_version=2)
    elif fault == "boot":
        w.host.boot_manifest_provenance = lambda: None
    elif fault == "manifest":
        w.host.boot_manifest_path = None
    elif fault == "withdrawn":
        w.host.capabilities = {}
    else:
        monkeypatch.setattr(transport.shutil, "which", lambda value: None)
    with pytest.raises(transport.PreparationTransportError):
        REAL_VALIDATE_BINDING(w.host, config)


def test_source_unavailable_before_delegation_and_readiness_withdrawn(world):
    w = world
    listener = w.host.start_storage_preparation(w.config)
    w.source_valid = False
    assert exchange(w)["refusal"] == {"code": "source_mismatch"}
    assert not w.calls
    assert not listener.is_ready()
    assert not w.config.readiness_path.exists()


def test_unsupported_peer_authentication_has_no_uid_only_fallback(world, monkeypatch):
    def unavailable(*args, **kwargs):
        raise custody.CustodyError("Darwin audit-token custody is unavailable")
    monkeypatch.setattr(custody.AuthenticatedCleanupActor, "current", unavailable)
    with pytest.raises(custody.CustodyError, match="unavailable"):
        world.host.start_storage_preparation(world.config)
    assert not world.config.readiness_path.exists()


def test_shutdown_interrupts_incomplete_connection(world):
    w = world
    listener = w.host.start_storage_preparation(w.config)
    client, server = socket.socketpair()
    w.accepts.put(server)
    client.sendall(b"{")
    deadline = time.monotonic() + 1
    while listener._active is None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert listener._active is not None
    w.host.shutdown()
    client.close()
    assert not listener._thread.is_alive()
    assert not w.config.readiness_path.exists()
    assert not w.calls


def test_input_bearing_capability_readiness_refuses_before_any_estimation(admitted):
    w = admitted
    w.record.definition.metadata["hc04_cas_param_ports"] = ["managed"]
    # Updating the trusted test definition pin isolates the input frontier.
    config = replace(w.config, capability_digest=hosts._capability_digest(w.record.definition.to_dict()))
    w.host.capabilities[w.record.id] = replace(w.record, capability_digest=config.capability_digest)
    with pytest.raises(transport.PreparationTransportError, match="input_facts_unavailable"):
        REAL_VALIDATE_BINDING(w.host, config)
    assert not w.calls


def test_estimator_failure_keeps_public_refusal_and_bounded_internal_diagnostic(world, capsys):
    w = world
    def fail(request):
        raise hosts.HostError("controlled failure " + "x" * 1000)
    w.host.prepare_storage_estimate = fail
    w.host.start_storage_preparation(w.config)
    assert exchange(w)["refusal"] == {"code": "preparation_unavailable"}
    event = json.loads(capsys.readouterr().err)
    assert event["event"] == "storage_preparation_failed"
    assert event["exception"] == "HostError"
    assert len(event["message"]) == 512
    assert "spec" not in event and "request" not in event
