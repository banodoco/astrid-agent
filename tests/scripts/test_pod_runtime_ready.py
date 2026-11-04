"""Offline strict live Runtime readiness and owner-fenced teardown fakes."""
import base64
import copy
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from tests.helpers.runtime import use_runtime


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/pod_runtime_ready.py"
spec = importlib.util.spec_from_file_location("pod_runtime_ready", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    use_runtime()
    from astrid_runtime_client.contract_metadata import SCHEMA_DIGEST

    for key in tuple(probe.os.environ):
        if key.startswith("ASTRID_TIMELINE_EVAL_") or key in {
                "ASTRID_RUNTIME_ENDPOINT", "ASTRID_RUNTIME_CREDENTIAL", "ASTRID_RUNTIME_LAUNCHER"}:
            monkeypatch.delenv(key)
    root, data = tmp_path / "sources", tmp_path / "support"
    agent = root / "astrid-agent"
    agent.mkdir(parents=True)
    lock = agent / "runtime.lock.json"
    lock.write_bytes(b"fixture exact Runtime lock\n")
    owner = {"runtime_instance_id": "runtime-1", "realm_id": "realm-1", "pid": 12,
             "process_birth_id": "birth-12", "realm_root": str(tmp_path / "realm")}
    (data / "runtime/source-profiles").mkdir(parents=True)
    (data / "runtime/source-profiles/astrid.json").write_text("{}")
    (data / "runtime/instance.lock").write_text(json.dumps(owner))
    identity = {"release": 0, "managed_by": "source", "runtime_head": "1" * 40,
                "agent_head": "2" * 40, "dirty": False, "dirty_hash": "3" * 64}
    request = {"pod_id": "pod1", "volume_id": "volume-1", "nonce": "4" * 32,
               "runtime_head": identity["runtime_head"],
               "runtime_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest()}
    health = {"status": "ok", "protocol": "workspace.v1", "identity": identity,
              "schema_digest": SCHEMA_DIGEST, "runtime_epoch": 1,
              "runtime_instance_id": "runtime-1", "runtime_session_id": "session-1"}
    handshake = {"protocol": "workspace.v1", "schema_digest": SCHEMA_DIGEST,
                 "session_id": "handshake-1", "realm_id": "realm-1", "actor_id": "actor-1",
                 "scopes": ["projects:read", "projects:write", "objects:read", "objects:write",
                            "tasks:read", "tasks:write"],
                 "capabilities": ["execution_binding.targeted.v1"]}
    endpoint = "http://127.0.0.1:17341"
    status = {"discovery": {**owner, "active_realm": "realm-1", "identity": identity,
                            "endpoint": endpoint},
              "health": health, "support": {"healthy": True, "realm_id": "realm-1"}}
    state = SimpleNamespace(root=root, data=data, request=request, owner=owner, identity=identity,
                            health=health, handshake=handshake, status=status, events=[],
                            stop={"status": "stopped", "realm_id": "realm-1"}, stop_code=0,
                            start_error=None, action_roots=[])

    @contextlib.contextmanager
    def action_runtime(workspace):
        # Unit collaborator only: the installed-wheel regression runs the real
        # action setup/launcher. Keep these roots distinct so the live probe
        # cannot accidentally inspect the bootstrap workspace's owner instead.
        assert probe.os.environ["ASTRID_RUNTIME_SOURCE_MANIFEST"] == str(data / "runtime/source-profiles/astrid.json")
        action_data = workspace / "support"
        (action_data / "runtime").mkdir(parents=True)
        (action_data / "runtime/instance.lock").write_text(json.dumps(state.owner))
        state.action_roots.append(workspace)
        state.action_data, state.action_manifest = action_data, workspace / "source-profile.json"
        yield dict(probe.os.environ, ASTRID_RUNTIME_DATA_ROOT=str(action_data),
                   ASTRID_RUNTIME_SOURCE_MANIFEST=str(state.action_manifest))

    monkeypatch.setattr("astrid.core.action_cli.ephemeral_runtime", action_runtime)

    def verify(source_root, profile):
        state.events.append("static")
        assert source_root == root and profile == data / "runtime/source-profiles/astrid.json"
        return identity

    monkeypatch.setitem(sys.modules, "pod_source_identity", SimpleNamespace(verify=verify))

    def ensure_runtime(*, start_pack_host):
        state.events.append("up")
        assert start_pack_host is False
        assert probe.os.environ["ASTRID_RUNTIME_DATA_ROOT"] == str(state.action_data)
        assert probe.os.environ["ASTRID_RUNTIME_SOURCE_MANIFEST"] == str(state.action_manifest)
        assert probe.os.environ["ASTRID_RUNTIME_LAUNCHER"] == str(Path(sys.executable).with_name("astrid-runtime"))
        if state.start_error:
            raise state.start_error
        return {"endpoint": endpoint, "credential_file": "/fixture/credential-path-only",
                "realm_id": "realm-1", "actor_id": "actor-1"}

    class FakeWorkspace:
        def __init__(self, url, token):
            self.endpoint = url
            assert url == endpoint and token == "fixture-token"

        def health(self):
            state.events.append("health")
            return state.health

        def handshake(self, *args):
            state.events.append("handshake")
            return state.handshake

    def run(argv, **kwargs):
        verb = argv[1]
        state.events.append(verb)
        assert argv[:5] == [str(Path(sys.executable).with_name("astrid-runtime")), verb,
                            "--data-root", str(state.action_data), "--json"]
        if verb == "down":
            assert argv[5:] == ["--profile", "astrid"]
        else:
            assert verb == "status" and argv[5:] == []
        assert kwargs == {"capture_output": True, "text": True, "timeout": 180, "check": False}
        return SimpleNamespace(returncode=state.stop_code if verb == "down" else 0,
                               stdout=json.dumps(state.stop if verb == "down" else state.status))

    monkeypatch.setattr("astrid.sdk.autobootstrap.ensure_runtime", ensure_runtime)
    monkeypatch.setattr("astrid.sdk.workspace_client.resolve_runtime_connection", lambda url, credential: (url, "fixture-token"))
    monkeypatch.setattr("astrid.sdk.workspace_client.WorkspaceClient", FakeWorkspace)
    monkeypatch.setattr(probe.subprocess, "run", run)
    return state


def test_live_probe_uses_strict_handshake_and_stops_before_return(rig, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", "/prior-root")
    result = probe.runtime_probe(rig.request, source_root=rig.root, data_root=rig.data)
    assert rig.events == ["static", "up", "health", "handshake", "status", "down"]
    assert result == {**rig.request, "identity": rig.identity, "runtime_instance_id": "runtime-1",
                      "realm_id": "realm-1", "ready": True, "stopped": True}
    assert probe.os.environ["ASTRID_RUNTIME_DATA_ROOT"] == "/prior-root"
    assert "ASTRID_RUNTIME_LAUNCHER" not in probe.os.environ
    assert len(rig.action_roots) == 1
    assert not rig.action_roots[0].exists()


@pytest.mark.parametrize("field,value", [
    ("runtime_head", "9" * 40), ("runtime_lock_sha256", "9" * 64),
])
def test_controller_identity_failure_never_starts_runtime(rig, field, value):
    rig.request[field] = value
    with pytest.raises(ValueError, match="controller's exact lock"):
        probe.runtime_probe(rig.request, source_root=rig.root, data_root=rig.data)
    assert rig.events == ["static"]


@pytest.mark.parametrize("part,field,value", [
    ("health", "status", "unready"), ("health", "schema_digest", "wrong"),
    ("health", "identity", {"managed_by": "source"}),
    ("handshake", "realm_id", "other"), ("handshake", "actor_id", "other"),
    ("handshake", "protocol", "other"), ("handshake", "scopes", []),
    ("handshake", "capabilities", []),
])
def test_real_sdk_health_and_handshake_failure_stops_runtime(rig, part, field, value):
    getattr(rig, part)[field] = value
    with pytest.raises(Exception):
        probe.runtime_probe(rig.request, source_root=rig.root, data_root=rig.data)
    assert rig.events[-1] == "down"
    assert "status" not in rig.events


@pytest.mark.parametrize("part,field,value", [
    ("discovery", "runtime_instance_id", "other"), ("discovery", "endpoint", "http://127.0.0.1:9"),
    ("discovery", "active_realm", "other"), ("discovery", "process_birth_id", "other"),
    ("discovery", "realm_root", "/other"), ("discovery", "identity", {}),
    ("support", "healthy", False), ("support", "realm_id", "other"),
    ("health", "runtime_instance_id", "other"),
])
def test_live_receipt_mismatch_stops_runtime(rig, part, field, value):
    # Keep SDK health valid; mutate only the subsequent status observation.
    rig.status = copy.deepcopy(rig.status)
    rig.status[part][field] = value
    with pytest.raises(Exception):
        probe.runtime_probe(rig.request, source_root=rig.root, data_root=rig.data)
    assert rig.events[-2:] == ["status", "down"]


@pytest.mark.parametrize("cleanup", ["exit", "status", "realm"])
def test_cleanup_must_be_confirmed_before_readiness(rig, cleanup):
    if cleanup == "exit":
        rig.stop_code = 1
    elif cleanup == "status":
        rig.stop["status"] = "pending"
    else:
        rig.stop["realm_id"] = "other"
    with pytest.raises(ValueError, match="cleanup"):
        probe.runtime_probe(rig.request, source_root=rig.root, data_root=rig.data)
    assert rig.events[-1] == "down"


def test_launcher_failure_still_attempts_fenced_cleanup(rig):
    from astrid.sdk.autobootstrap import AutoBootstrapError

    rig.start_error = AutoBootstrapError("fixture startup failed")
    with pytest.raises(Exception):
        probe.runtime_probe(rig.request, source_root=rig.root, data_root=rig.data)
    assert rig.events == ["static", "up", "down"]


@pytest.mark.parametrize("name", ["ASTRID_RUNTIME_ENDPOINT", "ASTRID_RUNTIME_CREDENTIAL",
                                 "ASTRID_RUNTIME_LAUNCHER", "ASTRID_TIMELINE_EVAL_ENDPOINT"])
def test_ambient_connection_override_is_rejected(rig, monkeypatch, name):
    monkeypatch.setenv(name, "untrusted")
    with pytest.raises(ValueError, match="ambient Runtime"):
        probe.runtime_probe(rig.request, source_root=rig.root, data_root=rig.data)
    assert rig.events == ["static"]


def test_cli_sanitizes_runtime_failure(rig, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("fixture-credential-must-not-appear")

    monkeypatch.setattr(probe, "runtime_probe", fail)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), base64.b64encode(json.dumps(rig.request).encode()).decode()])
    with pytest.raises(SystemExit, match="pod Runtime identity/readiness verification failed") as exc:
        probe.main()
    assert "fixture-credential" not in str(exc.value)
    assert capsys.readouterr().out == ""
