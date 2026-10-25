"""F05 ordinary v3 actions use the existing admitted host child boundary."""
from __future__ import annotations

import json
import os
import py_compile
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from astrid.core.execution.generic_host import GenericPackHost, HostCancelled, HostError
from astrid.core.execution.executor.runner import ExecutorRunRequest, run_executor
from astrid.core.execution.executor.registry import ExecutorRegistry
from tests.test_generic_host import FakeRuntime as _CurrentFakeRuntime, _write_manifest


def _native_cpu_handoff_fixture(tmp_path, monkeypatch, *, graph_owned=True):
    """Real MTS and checkout containment; only HTTP and owner observations fake."""
    import threading
    from types import SimpleNamespace
    from astrid.core.execution.generic_host import _ManagedVibeSessionAdapter
    from astrid.core.execution.managed_tool_session import CapabilityDescriptor, SessionBinding
    from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter

    host = GenericPackHost(pack_roots=[tmp_path])
    graph = SimpleNamespace(handles=[object()], abort_calls=[])
    graph = graph if graph_owned else None
    host._native_engine_graph = graph
    backend = CheckoutServerAdapter("http://gpu.example.test")
    backend._host_session = {"managed": True, "session_dir": str(tmp_path / "session")}
    controls, observations, restarts = [], [], []
    monkeypatch.setattr(backend, "_revalidate_host_session", lambda: observations.append("owner"))
    monkeypatch.setattr(backend, "_probe_system_stats", lambda: observations.append("stats"))
    monkeypatch.setattr(backend, "_restart_owned_session", lambda: restarts.append("restart"))
    def post(path, payload):
        controls.append((path, payload))
        return {"acknowledged": True} if path == "/api/free" else {}
    monkeypatch.setattr(backend._engine, "_post", post)
    bridge = _ManagedVibeSessionAdapter(backend, graph)
    binding = SessionBinding("native", "runtime", "birth", "http://gpu.example.test", "source", "config")
    host.managed_tool_session.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=binding, adapter=bridge)
    token = host.managed_tool_session.admit(capability_id="vibecomfy.run", invocation_id="native-task")
    host.managed_tool_session.adapter_for(token, begin=True)
    _settle_cpu_handoff_token(host.managed_tool_session, token)
    host._record_native_cpu_handoff(bridge, token, managed_settled=True, runtime_settled=True)
    for name in ("_vibecomfy_warmth_hint", "_vibecomfy_current_warmth_hint", "_vibecomfy_requested_warmth_hint"):
        setattr(host, name, "previous-model")
    backend._engine._warm = True
    return SimpleNamespace(host=host, backend=backend, bridge=bridge, graph=graph, token=token,
                           controls=controls, observations=observations, restarts=restarts, threading=threading)


def _settle_cpu_handoff_token(manager, token):
    return manager.settle(token, result_evidence={"generation": token.generation,
        "binding_identity": list(token.binding_identity), "token_id": token.token_id,
        "invocation_id": token.invocation_id})


def _open_cpu_handoff(fixture, *, resources=("cpu",), adapter=None):
    from astrid.core.execution.generic_host import _ManagedTaskAdapter
    from astrid.core.execution.managed_tool_session import CapabilityDescriptor, SessionBinding
    target = adapter or _ManagedTaskAdapter(fixture.threading.Event())
    fixture.host._open_managed_task_session(capability=CapabilityDescriptor("compose", resources_claimed=resources),
        binding=SessionBinding("cpu", "runtime", "cpu-birth", "cpu", "source", "config"), adapter=target)
    return target


@pytest.mark.parametrize("completion", [{"models_unloaded": True}, {"acknowledged": True},
                                      {"completed": True, "status": "completed"}])
def test_native_cpu_handoff_uses_real_containment_without_owner_restart(tmp_path, monkeypatch, completion):
    from astrid.core.execution.managed_tool_session import StaleAdmissionError
    f = _native_cpu_handoff_fixture(tmp_path, monkeypatch)
    def post(path, payload):
        f.controls.append((path, payload))
        return completion if path == "/api/free" else {}
    monkeypatch.setattr(f.backend._engine, "_post", post)
    target = _open_cpu_handoff(f)
    assert f.host.managed_tool_session.current_adapter is target
    assert [path for path, _ in f.controls] == ["/interrupt", "/queue", "/api/free", "/queue", "/api/free"]
    assert f.observations == ["owner", "stats", "owner", "stats"]
    assert f.restarts == [] and f.host._native_engine_graph is f.graph
    assert f.bridge.native_graph is f.graph and len(f.graph.handles) == 1 and f.graph.abort_calls == []
    assert not f.backend._engine.warm
    assert all(getattr(f.host, name) is None for name in
        ("_vibecomfy_warmth_hint", "_vibecomfy_current_warmth_hint", "_vibecomfy_requested_warmth_hint"))
    with pytest.raises(StaleAdmissionError):
        _settle_cpu_handoff_token(f.host.managed_tool_session, f.token)


@pytest.mark.parametrize("fault", ["cancel_interrupt", "cancel_queue", "cancel_free", "cancel_owner",
                                  "release_queue", "release_free", "release_owner", "missing_completion", "false_completion"])
def test_native_cpu_handoff_failed_control_remains_fenced_occupied(tmp_path, monkeypatch, fault):
    f = _native_cpu_handoff_fixture(tmp_path, monkeypatch)
    calls = 0
    def owner():
        nonlocal calls
        calls += 1
        if fault == ("cancel_owner" if calls == 1 else "release_owner"):
            raise RuntimeError("ownership changed")
    monkeypatch.setattr(f.backend, "_revalidate_host_session", owner)
    def post(path, payload):
        f.controls.append((path, payload))
        phase = "cancel" if calls == 1 else "release"
        suffix = {"/interrupt": "interrupt", "/queue": "queue", "/api/free": "free"}[path]
        if fault == phase + "_" + suffix:
            raise OSError("control failed")
        if path == "/api/free":
            if phase == "release" and fault == "missing_completion": return {}
            if phase == "release" and fault == "false_completion": return {"acknowledged": False}
            return {"acknowledged": True}
        return {}
    monkeypatch.setattr(f.backend._engine, "_post", post)
    with pytest.raises(Exception, match="fencing was not verified|release was not verified"):
        _open_cpu_handoff(f)
    manager = f.host.managed_tool_session
    assert manager.occupied and not manager.active and manager.current_adapter is f.bridge
    assert f.bridge.cpu_handoff_failed and f.backend.poisoned
    before = list(f.controls)
    with pytest.raises(HostError, match="unresolved"):
        f.bridge.release(reason="cpu_residency_eviction")
    assert f.controls == before and f.restarts == [] and f.host._native_engine_graph is f.graph
    if fault.startswith("cancel_"):
        assert calls == 1  # failed fence cannot become a successful release retry


@pytest.mark.parametrize("fault", ["managed_unsettled", "runtime_unsettled", "shutdown",
                                  "gate_closed", "observation_unknown", "graph_changed", "generation_changed",
                                  "pending", "completed_intervening", "fenced"])
def test_native_cpu_handoff_rejects_ineligible_or_stale_authorization(tmp_path, monkeypatch, fault):
    f = _native_cpu_handoff_fixture(tmp_path, monkeypatch)
    manager = f.host.managed_tool_session
    if fault in {"managed_unsettled", "runtime_unsettled"}:
        f.host._record_native_cpu_handoff(f.bridge, f.token,
            managed_settled=fault != "managed_unsettled", runtime_settled=fault != "runtime_unsettled")
    elif fault == "shutdown": f.host._shutdown.set()
    elif fault == "gate_closed": f.host._handoff_claim_gate_closed = True
    elif fault == "observation_unknown": f.host._handoff_observation_known = False
    elif fault == "graph_changed": f.host._native_engine_graph = object()
    elif fault == "generation_changed": manager._generation += 1
    elif fault in {"pending", "completed_intervening"}:
        newer = manager.admit(capability_id="vibecomfy.run", invocation_id="newer")
        if fault == "completed_intervening": _settle_cpu_handoff_token(manager, newer)
    elif fault == "fenced": manager.fence(reason="test-fence")
    before = list(f.controls)
    with pytest.raises(HostError, match="exact settled ownership"):
        _open_cpu_handoff(f)
    assert f.controls == before and manager.current_adapter is f.bridge and manager.occupied


def test_native_cpu_handoff_late_finally_cannot_erase_fresh_marker(tmp_path, monkeypatch):
    f = _native_cpu_handoff_fixture(tmp_path, monkeypatch)
    newer = f.host.managed_tool_session.admit(capability_id="vibecomfy.run", invocation_id="newer")
    _settle_cpu_handoff_token(f.host.managed_tool_session, newer)
    f.host._record_native_cpu_handoff(f.bridge, newer, managed_settled=True, runtime_settled=True)
    f.host._record_native_cpu_handoff(f.bridge, f.token, managed_settled=False, runtime_settled=False)
    assert f.bridge.cpu_handoff_token is newer
    target = _open_cpu_handoff(f)
    assert f.host.managed_tool_session.current_adapter is target


@pytest.mark.parametrize("resources", [("gpu",), ("cpu", "gpu"), ("model",)])
def test_native_cpu_handoff_does_not_authorize_native_model_replacement(tmp_path, monkeypatch, resources):
    from astrid.core.execution.managed_tool_session import SessionCapacityError
    f = _native_cpu_handoff_fixture(tmp_path, monkeypatch)
    with pytest.raises(SessionCapacityError, match="release was not verified"):
        _open_cpu_handoff(f, resources=resources)
    assert f.host.managed_tool_session.occupied and f.restarts == [] and f.bridge.native_graph is f.graph


def test_native_cpu_handoff_preserves_legacy_capacity_replacement(tmp_path, monkeypatch):
    f = _native_cpu_handoff_fixture(tmp_path, monkeypatch, graph_owned=False)
    monkeypatch.setattr(f.backend, "_restart_owned_session", lambda: f.restarts.append("restart") or
                        {"ok": True, "released": True})
    target = _open_cpu_handoff(f)
    assert f.host.managed_tool_session.current_adapter is target
    assert f.restarts == ["restart"]


def _sdk_cpu_session_fixture(tmp_path, monkeypatch):
    """Real HC03 factory/composite verifier; fake only OS and HTTP observations."""
    import hashlib
    import io
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    import vibecomfy.runtime.session as session_module
    from astrid.core.generation.backends import vibecomfy as backend_module
    from astrid.core.generation.model_root import canonical_model_inventory_digest

    root = tmp_path / "sdk-owner"
    root.mkdir()
    output = root / "outputs"
    output.mkdir()
    models = root / "models"
    models.mkdir()
    model = models / "fixture.safetensors"
    model.write_bytes(b"explicit synthetic unit model")
    inventory = [{"name": model.name, "sha256": "sha256:" + hashlib.sha256(model.read_bytes()).hexdigest(),
                  "size": model.stat().st_size, "subdir": "."}]
    session = root / "session"
    session.mkdir()
    daemon, child = 1900001, 1900002
    birth, child_birth = "fixture-daemon-birth", "fixture-child-birth"
    url, token = "http://127.0.0.1:18188", "fixture-launch-token"
    # The attested revision must be the one the selected VibeComfy checkout is pinned to
    # (ASTRID_VIBECOMFY_CHECKOUT is set in CI), so use the product's pin rather than a made-up one.
    from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
    revision, content = VIBECOMFY_ENGINE_REVISION, "sha256:" + hashlib.sha256(b"unit source fixture").hexdigest()
    config = json.dumps({"output_directory": str(output)}, sort_keys=True).encode()
    marker = {"pid": daemon, "comfy_pid": child, "process_start_identity": birth,
              "comfy_process_start_identity": child_birth, "url": url, "launch_token": token}
    registry = {"pid": str(daemon), "comfy_pid": str(child), "comfy_process_start_identity": child_birth,
                "url": url, "config.json": config, "launch.json": json.dumps(marker),
                "source_revision": revision, "source_content_digest": content, "daemon.log": "unit-owner\n"}
    for name, value in registry.items():
        (session / name).write_bytes(value if isinstance(value, bytes) else value.encode())
    exact = {"interpreter": "python", "runtime_lock": "sha256:" + "1" * 64,
             "engine_lock": "sha256:" + "2" * 64, "model_digest": inventory[0]["sha256"],
             "custom_node_digest": "sha256:" + "3" * 64, "driver": "synthetic CPU",
             "root": "sha256:" + "4" * 64, "port": 18188}
    facts = {"exact": exact, "minimum": {"vram_bytes": 0, "scratch_bytes": 8192}}
    profile = {"schema_version": "hc03-worker-readiness.v1", "status": "ready", "verified_facts": facts,
               "verified_facts_digest": backend_module._canonical_sha256(facts),
               "runtime": {"runtime_instance_id": "00000000-0000-4000-8000-000000000001"},
               "launch": {"output_root": str(output), "model_root": {"schema_version": 1, "path": str(models),
                          "inventory": inventory, "inventory_digest": canonical_model_inventory_digest(inventory)}},
               "vibecomfy_session": {"session_dir": str(session), "server_url": url, "pid": daemon,
                   "comfy_pid": child, "launch_token": token, "process_birth_id": birth,
                   "comfy_process_birth_id": child_birth, "source_revision": revision,
                   "source_content_digest": content, "config_digest": "sha256:" + hashlib.sha256(config).hexdigest()}}
    observations = {"listener": child, "parent": daemon, "birth": birth, "child_birth": child_birth}
    ownership_reads, controls, signals = [], [], []
    def observed_birth(pid):
        ownership_reads.append(pid)
        return observations["birth"] if pid == daemon else observations["child_birth"] if pid == child else None
    monkeypatch.setattr(session_module, "_process_start_identity", observed_birth)
    monkeypatch.setattr(session_module, "_process_commandline", lambda pid: ("python", "--launch-token", token))
    monkeypatch.setattr(session_module, "_process_parent_pid", lambda pid: observations["parent"])
    monkeypatch.setattr(session_module, "_listener_owner_pid", lambda endpoint: observations["listener"])
    original_kill = os.kill
    def observed_kill(pid, sig):
        if pid in (daemon, child):
            signals.append((pid, sig))
            assert sig == 0, "residency control must not signal the external owner"
            return None
        return original_kill(pid, sig)
    monkeypatch.setattr(os, "kill", observed_kill)
    observed_identity = backend_module.platform.process_identity
    identity_reads = []
    def native_identity(pid):
        if pid in (daemon, child):
            identity_reads.append(pid)
            return {"pid": pid, "birth_id": birth if pid == daemon else child_birth}
        return observed_identity(pid)
    monkeypatch.setattr(backend_module.platform, "process_identity", native_identity)
    monkeypatch.setenv("ASTRID_VIBECOMFY_ATTESTED_REVISION", revision)
    monkeypatch.setenv("ASTRID_VIBECOMFY_ATTESTED_CONTENT_DIGEST", content)
    class Response(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    http = {"completion": {"completed": True, "status": "completed"}, "failure": None, "failure_call": None, "on_control": None}
    def opened(request, **kwargs):
        path = urlsplit(request.full_url).path
        if request.data is not None:
            controls.append(path)
            if http["on_control"] is not None:
                http["on_control"](len(controls))
            if http["failure"] == path and http["failure_call"] in (None, len(controls)):
                raise OSError("synthetic control transport failure")
            body = http["completion"] if path == "/api/free" else {"ok": True}
        else:
            assert path == "/system_stats"
            body = {"system": {"comfyui_version": "0.26.0"}}
        return Response(json.dumps(body).encode())
    monkeypatch.setattr(backend_module, "_open_checkout_http", opened)
    assert session_module._session_composite_ownership_verified(session, daemon) is True
    return SimpleNamespace(profile=profile, session=session, daemon=daemon, child=child,
                           observations=observations, ownership_reads=ownership_reads, controls=controls,
                           signals=signals, identity_reads=identity_reads, http=http, root=root)


def _sdk_cpu_handoff_fixture(tmp_path, monkeypatch):
    import threading
    from astrid.core.execution.generic_host import _ManagedVibeSessionAdapter
    from astrid.core.execution.managed_tool_session import CapabilityDescriptor, SessionBinding
    f = _sdk_cpu_session_fixture(tmp_path, monkeypatch)
    f.threading = threading
    f.host = GenericPackHost(pack_roots=[tmp_path])
    f.host._native_engine_graph = None
    f.bridge = _ManagedVibeSessionAdapter.from_host_session(hc03_profile=f.profile,
        model_id="fixture-model", template_id="fixture-template", invocation_identity="unit-task")
    s = f.profile["vibecomfy_session"]
    binding = SessionBinding(s["session_dir"], f.profile["runtime"]["runtime_instance_id"],
                             s["process_birth_id"], s["server_url"], s["source_revision"], s["config_digest"],
                             engine_birth_id=s["comfy_process_birth_id"])
    manager = f.host.managed_tool_session
    manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=binding, adapter=f.bridge)
    f.token = manager.admit(capability_id="vibecomfy.run", invocation_id="unit-task")
    manager.adapter_for(f.token, begin=True)
    _settle_cpu_handoff_token(manager, f.token)
    f.host._record_native_cpu_handoff(f.bridge, f.token, managed_settled=True, runtime_settled=True)
    return f


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
def test_sdk_cpu_handoff_reaches_real_run_task_finally_without_native_graph(tmp_path, monkeypatch):
    import ast
    import hashlib
    import inspect
    from astrid.core.execution import generic_host as module
    from astrid.sdk import host_bootstrap
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle
    from vibecomfy.security.provenance import Provenance

    # Derive the actual SDK argv; this route deliberately has no activation graph.
    tree = ast.parse(inspect.getsource(host_bootstrap.ensure_pack_host))
    argv = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "argv" for target in node.targets))
    flags = {node.value for node in ast.walk(argv) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert "--register" in flags and not flags.intersection({"--host-control-fd", "--activation-fd", "--activation-socket"})
    f = _sdk_cpu_session_fixture(tmp_path, monkeypatch)
    profile_path = tmp_path / "readiness.json"
    profile_path.write_text(json.dumps(f.profile))
    monkeypatch.setenv("ASTRID_HOST_READINESS_PROFILE_PATH", str(profile_path))
    monkeypatch.setenv("ASTRID_HOST_READINESS_PROFILE_HASH", "sha256:" + hashlib.sha256(profile_path.read_bytes()).hexdigest())
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    workflow = VibeWorkflow("sdk-cpu-handoff", WorkflowSource("sdk-cpu-handoff"),
                            metadata={"model_id": "fixture-model", "ready_template": "fixture-template"})
    workflow.add_node("Integer", uid="value", value=7)
    emit_bundle(workflow, bundle_root / "workflow.py", provenance=Provenance.AGENT_AUTHORED)
    (bundle_root / "source.json").write_text('{"nodes":[],"links":[]}\n')
    objects, digests = {}, []
    for name, basename in (("python", "workflow.py"), ("companion", "workflow.vibe.json"), ("source", "source.json")):
        data = (bundle_root / basename).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        objects[digest] = data
        digests.append({"name": name, "digest": digest})
    packs = tmp_path / "packs"
    for capability, family in (("vibecomfy.run", "local_generation"), ("h3_av.compose", "cpu")):
        path = packs / capability
        path.mkdir(parents=True)
        definition = {"schema_version": 1, "id": capability, "name": capability, "kind": "external", "version": "1.0",
            "command": {"argv": ["{python_exec}", "-c", "from pathlib import Path; p=Path('{out}'); p.mkdir(parents=True,exist_ok=True); (p/'result.txt').write_text('settled')"]},
            "inputs": [{"name": name, "type": "file"} for name in ("python", "companion", "source")] if family == "local_generation" else [],
            "outputs": [{"name": "result", "type": "file", "path_template": "{out}/result.txt", "artifact_type": "text/plain"}],
            "metadata": {"adapter_family": family, "resource_keys": ["gpu"] if family == "local_generation" else ["cpu"]}}
        (path / "executor.yaml").write_text(json.dumps(definition))
    runtime = FakeRuntime()
    runtime.get_object = objects.__getitem__
    host = GenericPackHost(pack_roots=[packs], client=runtime, attempt_base=tmp_path / "attempts")
    host._native_engine_graph = None
    host.discover()
    host.preflight()
    assert all(record.ready for record in host.capabilities.values())
    before = {p.name: p.read_bytes() for p in f.session.iterdir()}
    native = {"task": {"id": "vibe", "capability": "vibecomfy.run", "project_id": "unit-project",
        "attempt_id": "vibe-attempt", "fence": 1, "input_object_ids": list(objects),
        "spec": {"spec": {"inputs": {}, "input_digests": digests}}}}
    runtime.tasks["vibe"] = native
    assert host.run_task(native, lease_token="unit-lease")["task"]["status"] == "completed"
    bridge = host.managed_tool_session.current_adapter
    assert bridge.native_graph is None
    assert runtime.settlements[0][2]["result"]["managed_tool_session"]["state"] == "settled"
    assert not host._unresolved_task_scopes
    compose = {"task": {"id": "compose", "capability": "h3_av.compose", "project_id": "unit-project",
                        "attempt_id": "compose-attempt", "fence": 1, "spec": {"spec": {"inputs": {}}}}}
    runtime.tasks["compose"] = compose
    assert host.run_task(compose, lease_token="unit-lease")["task"]["status"] == "completed"
    assert bridge.sdk_session_identity is not None
    assert len(runtime.settlements) == 2 and runtime.failures == []
    assert f.controls == ["/interrupt", "/queue", "/api/free", "/queue", "/api/free"]
    assert len(f.ownership_reads) > 8  # Real composite verifier ran at admission and both controls.
    assert {p.name: p.read_bytes() for p in f.session.iterdir()} == before
    assert f.identity_reads and all(pid in (f.daemon, f.child) for pid in f.identity_reads)
    assert all(sig == 0 for _, sig in f.signals)
    assert host._native_engine_graph is None and not host.managed_tool_session.occupied
    assert all(getattr(host, name) is None for name in ("_vibecomfy_warmth_hint", "_vibecomfy_current_warmth_hint", "_vibecomfy_requested_warmth_hint"))


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
@pytest.mark.parametrize("field", ["session", "profile", "model", "template", "runtime", "fingerprint", "backend", "lost_binding", "invocation"])
def test_sdk_cpu_handoff_binding_drift_never_restarts(tmp_path, monkeypatch, field):
    f = _sdk_cpu_handoff_fixture(tmp_path, monkeypatch)
    backend = f.bridge.backend
    if field == "session": backend._host_session["config_digest"] = "sha256:" + "0" * 64
    elif field == "profile": backend._host_profile["launch"]["output_root"] = str(tmp_path)
    elif field == "model": backend._bound_model_id = "different-model"
    elif field == "template": backend._bound_template_id = "different-template"
    elif field == "runtime": backend._runtime_instance_id = "00000000-0000-4000-8000-000000000002"
    elif field == "fingerprint": backend._bound_fingerprint = "different-fingerprint"
    elif field == "backend": f.bridge.backend = object()
    elif field == "lost_binding": backend._host_session = None
    elif field == "invocation": backend._invocation_identity = "changed-task"
    with pytest.raises(HostError, match="exact settled ownership"):
        _open_cpu_handoff(f)
    assert f.controls == [] and f.host.managed_tool_session.current_adapter is f.bridge


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
@pytest.mark.parametrize("fault", ["listener", "parent", "birth", "child_birth", "cancel_transport", "release_transport", "completion"])
def test_sdk_cpu_handoff_canonical_ownership_and_control_fail_closed(tmp_path, monkeypatch, fault):
    from astrid.core.execution.managed_tool_session import SessionCapacityError
    f = _sdk_cpu_handoff_fixture(tmp_path, monkeypatch)
    if fault in f.observations: f.observations[fault] = "changed" if "birth" in fault else -1
    elif fault in {"cancel_transport", "release_transport"}:
        f.http.update(failure="/api/free", failure_call=3 if fault == "cancel_transport" else 5)
    elif fault == "completion": f.http["completion"] = {"ok": True}
    with pytest.raises((HostError, SessionCapacityError)):
        _open_cpu_handoff(f)
    assert f.bridge.cpu_handoff_failed and f.host.managed_tool_session.occupied
    assert f.host.managed_tool_session._active.fenced
    controls = list(f.controls)
    with pytest.raises(HostError, match="unresolved"):
        f.bridge.release(reason="cpu_residency_eviction")
    assert f.controls == controls


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
@pytest.mark.parametrize("fault", ["completed_intervening", "pending", "shutdown", "unknown", "gate", "fenced", "generation"])
def test_sdk_cpu_handoff_actual_binding_keeps_atomic_refusals(tmp_path, monkeypatch, fault):
    f = _sdk_cpu_handoff_fixture(tmp_path, monkeypatch)
    manager = f.host.managed_tool_session
    if fault in {"completed_intervening", "pending"}:
        newer = manager.admit(capability_id="vibecomfy.run", invocation_id="intervening")
        if fault == "completed_intervening": _settle_cpu_handoff_token(manager, newer)
    elif fault == "shutdown": f.host._shutdown.set()
    elif fault == "unknown": f.host._handoff_observation_known = False
    elif fault == "gate": f.host._handoff_claim_gate_closed = True
    elif fault == "fenced": manager._active.fenced = True
    elif fault == "generation": manager._generation += 1
    with pytest.raises(HostError, match="exact settled ownership"):
        _open_cpu_handoff(f)
    assert f.controls == [] and manager.occupied


@pytest.mark.parametrize("lost", [None, "changed"])
def test_native_cpu_handoff_lost_graph_cannot_downgrade_to_sdk(tmp_path, monkeypatch, lost):
    f = _native_cpu_handoff_fixture(tmp_path, monkeypatch)
    f.bridge.native_graph = f.host._native_engine_graph = None if lost is None else object()
    with pytest.raises(HostError, match="exact settled ownership"):
        _open_cpu_handoff(f)
    assert f.controls == [] and f.host.managed_tool_session.occupied


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
@pytest.mark.parametrize("settlement", ["managed", "runtime"])
def test_sdk_cpu_handoff_requires_both_settlements(tmp_path, monkeypatch, settlement):
    f = _sdk_cpu_handoff_fixture(tmp_path, monkeypatch)
    f.host._record_native_cpu_handoff(f.bridge, f.token,
        managed_settled=settlement != "managed", runtime_settled=settlement != "runtime")
    with pytest.raises(HostError, match="exact settled ownership"):
        _open_cpu_handoff(f)
    assert not f.controls and f.host.managed_tool_session.occupied


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
def test_sdk_cpu_handoff_late_finally_preserves_fresh_token(tmp_path, monkeypatch):
    f = _sdk_cpu_handoff_fixture(tmp_path, monkeypatch)
    newer = f.host.managed_tool_session.admit(capability_id="vibecomfy.run", invocation_id="newer")
    _settle_cpu_handoff_token(f.host.managed_tool_session, newer)
    f.host._record_native_cpu_handoff(f.bridge, newer, managed_settled=True, runtime_settled=True)
    f.host._record_native_cpu_handoff(f.bridge, f.token, managed_settled=False, runtime_settled=False)
    assert f.bridge.cpu_handoff_token is newer
    _open_cpu_handoff(f)


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
@pytest.mark.parametrize("resources", [("gpu",), ("cpu", "gpu"), ("model",)])
def test_sdk_cpu_handoff_never_authorizes_native_model_restart(tmp_path, monkeypatch, resources):
    from astrid.core.execution.managed_tool_session import SessionCapacityError
    f = _sdk_cpu_handoff_fixture(tmp_path, monkeypatch)
    with pytest.raises(SessionCapacityError, match="release was not verified"):
        _open_cpu_handoff(f, resources=resources)
    assert f.host.managed_tool_session.occupied
    assert f.controls == ["/interrupt", "/queue", "/api/free"]


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
@pytest.mark.parametrize("field", ["source_revision", "source_content_digest", "config.json", "launch.json"])
def test_sdk_cpu_handoff_factory_rejects_registry_drift(tmp_path, monkeypatch, field):
    from astrid.core.execution.generic_host import _ManagedVibeSessionAdapter
    f = _sdk_cpu_session_fixture(tmp_path, monkeypatch)
    (f.session / field).write_text("different")
    with pytest.raises(Exception):
        _ManagedVibeSessionAdapter.from_host_session(hc03_profile=f.profile,
            model_id="fixture-model", template_id="fixture-template", invocation_identity="unit-task")
    assert not f.controls


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
def test_sdk_cpu_handoff_first_chain_is_redacted_before_secondary_cleanup(tmp_path, monkeypatch, caplog):
    f = _sdk_cpu_handoff_fixture(tmp_path, monkeypatch)
    def failed(**kwargs):
        try:
            raise OSError("SECRET arbitrary caller data")
        except OSError as cause:
            raise RuntimeError("SECRET secondary data") from cause
    monkeypatch.setattr(f.host.managed_tool_session, "_replace_quiescent", failed)
    with pytest.raises(RuntimeError, match="secondary"):
        _open_cpu_handoff(f)
    assert "CPU residency handoff first failure: runtime>io" in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
@pytest.mark.parametrize("invalid", ["absent", "partial", "unready"])
def test_sdk_cpu_handoff_factory_never_qualifies_unvalidated_profile(tmp_path, monkeypatch, invalid):
    from astrid.core.execution.generic_host import _ManagedVibeSessionAdapter
    f = _sdk_cpu_session_fixture(tmp_path, monkeypatch)
    profile = None if invalid == "absent" else {"schema_version": "hc03-worker-readiness.v1"} if invalid == "partial" else dict(f.profile, status="not-ready")
    with pytest.raises(ValueError):
        _ManagedVibeSessionAdapter.from_host_session(hc03_profile=profile,
            model_id="fixture-model", template_id="fixture-template", invocation_identity="unit-task")
    assert not f.controls


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
def test_sdk_cpu_handoff_serializes_real_control_admission_and_shutdown(tmp_path, monkeypatch):
    from astrid.core.execution.managed_tool_session import ManagedToolSessionError
    f = _sdk_cpu_handoff_fixture(tmp_path, monkeypatch)
    entered, proceed, admission_entered, shutdown_entered = [f.threading.Event() for _ in range(4)]
    outcomes = []
    def on_control(count):
        if count == 5:
            entered.set()
            assert proceed.wait(3)
    f.http["on_control"] = on_control
    def transition():
        try: _open_cpu_handoff(f); outcomes.append("transition")
        except BaseException as exc: outcomes.append(exc)
    def admit():
        admission_entered.set()
        try: f.host.managed_tool_session.admit(capability_id="vibecomfy.run", invocation_id="race")
        except ManagedToolSessionError: outcomes.append("admission-rejected")
    def shutdown_gate():
        shutdown_entered.set()
        with f.host.managed_tool_session._lock: f.host._shutdown.set()
        with f.host._handoff_condition: outcomes.append("shutdown-gate")
    threads = [f.threading.Thread(target=fn) for fn in (transition, admit, shutdown_gate)]
    try:
        threads[0].start(); assert entered.wait(3)
        threads[1].start(); threads[2].start()
        assert admission_entered.wait(3) and shutdown_entered.wait(3)
        assert "admission-rejected" not in outcomes and "shutdown-gate" not in outcomes
    finally:
        proceed.set()
        for thread in threads:
            if thread.ident is not None: thread.join(4)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["admission-rejected", "shutdown-gate", "transition"]
    assert f.controls == ["/interrupt", "/queue", "/api/free", "/queue", "/api/free"]
    assert f.identity_reads and all(pid in (f.daemon, f.child) for pid in f.identity_reads)
    assert all(sig == 0 for _, sig in f.signals)


def test_native_cpu_handoff_serializes_admission_and_shutdown_lock_order(tmp_path, monkeypatch):
    from astrid.core.execution.managed_tool_session import ManagedToolSessionError
    f = _native_cpu_handoff_fixture(tmp_path, monkeypatch)
    entered, proceed, admission_entered, shutdown_entered = [f.threading.Event() for _ in range(4)]
    outcomes = []
    calls = 0
    def post(path, payload):
        nonlocal calls
        calls += 1
        f.controls.append((path, payload))
        if calls == 5:
            entered.set()
            assert proceed.wait(3), "test control release was not unblocked"
        return {"acknowledged": True} if path == "/api/free" else {}
    monkeypatch.setattr(f.backend._engine, "_post", post)
    def transition():
        try: _open_cpu_handoff(f); outcomes.append("transition")
        except BaseException as exc: outcomes.append(exc)
    def admit():
        admission_entered.set()
        try: f.host.managed_tool_session.admit(capability_id="vibecomfy.run", invocation_id="race")
        except ManagedToolSessionError: outcomes.append("admission-rejected")
    def shutdown_gate():
        shutdown_entered.set()
        # Exact production order: release MTS before attempting handoff gate.
        with f.host.managed_tool_session._lock: f.host._shutdown.set()
        with f.host._handoff_condition: outcomes.append("shutdown-gate")
    threads = [f.threading.Thread(target=fn) for fn in (transition, admit, shutdown_gate)]
    try:
        threads[0].start(); assert entered.wait(3)
        threads[1].start(); threads[2].start()
        assert admission_entered.wait(3) and shutdown_entered.wait(3)
        assert "admission-rejected" not in outcomes and "shutdown-gate" not in outcomes
    finally:
        proceed.set()
        for thread in threads:
            if thread.ident is not None: thread.join(4)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["admission-rejected", "shutdown-gate", "transition"]


def test_host_dependency_projection_preserves_absent_runtime_origin(monkeypatch):
    from types import SimpleNamespace
    import astrid.core.execution.generic_host as module

    observed = []
    def spec(name):
        observed.append(name)
        return SimpleNamespace(origin="installed/astrid/__init__.py") if name == "astrid" else None
    monkeypatch.setattr(module.importlib.util, "find_spec", spec)
    assert module._host_product_import_origins() == {
        "astrid": "installed/astrid/__init__.py", "runtime_protocol": None,
    }
    assert observed == ["astrid", "runtime_protocol"]


@pytest.mark.integration  # needs sibling VibeComfy/Runtime checkout on sys.path
@pytest.mark.parametrize("case", ["installed", "source", "unconfigured", "profile_drift", "unattested",
                                  "candidate_drift", "unsupported_candidate", "alias", "cached_collision",
                                  "cached_submodule_collision", "resolution_collision", "ambient"])
def test_installed_host_vibe_imports_use_only_attested_root(tmp_path, case):
    """Exercise the helper in a fresh isolated process with controlled inputs.

    Qualification can supply its installed interpreter and attested clone;
    ordinary tests construct their own dependency and readiness document.
    """
    import hashlib
    import astrid.core.execution.generic_host as host_module

    python = os.environ.get("ASTRID_H3_HOST_IMPORT_TEST_PYTHON")
    retained = os.environ.get("ASTRID_H3_HOST_IMPORT_TEST_PROFILE")
    assert bool(python) == bool(retained), "qualification interpreter/profile must be paired"
    products = []
    if retained:
        profile = json.loads(Path(retained).read_text())
    else:
        import runtime_protocol

        python = sys.executable
        products = [str(Path(host_module.__file__).resolve().parents[3]),
                    str(Path(runtime_protocol.__file__).resolve().parent.parent)]
        fixture_checkout = tmp_path / "controlled-dependency"
        package = fixture_checkout / "vibecomfy"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('"""Controlled import fixture, no backend."""\n')
        (package / "workflow_bundle.py").write_text("FIXTURE_ONLY = True\n")
        revision = "1" * 40
        content = "sha256:" + hashlib.sha256((package / "workflow_bundle.py").read_bytes()).hexdigest()
        profile = {"vibecomfy_candidate": {"kind": "local_snapshot", "checkout": str(fixture_checkout),
                   "revision": revision, "source_content_digest": content},
                   "vibecomfy_session": {"source_revision": revision, "source_content_digest": content}}
    checkout = profile["vibecomfy_candidate"]["checkout"]
    selected = tmp_path / "readiness.json"
    if case == "candidate_drift":
        profile["vibecomfy_candidate"]["source_content_digest"] = "sha256:" + "0" * 64
    selected.write_text(json.dumps(profile))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(ASTRID_HOST_EXECUTION_PROFILE="source" if case == "source" else "installed",
               ASTRID_VIBECOMFY_CHECKOUT=checkout,
               ASTRID_HOST_READINESS_PROFILE_PATH=str(selected),
               ASTRID_HOST_READINESS_PROFILE_HASH="sha256:" + hashlib.sha256(selected.read_bytes()).hexdigest(),
               ASTRID_VIBECOMFY_ATTESTED_REVISION=profile["vibecomfy_session"]["source_revision"],
               ASTRID_VIBECOMFY_ATTESTED_CONTENT_DIGEST=profile["vibecomfy_session"]["source_content_digest"],
               ASTRID_VIBECOMFY_CANDIDATE_KIND="local_snapshot",
               ASTRID_VIBECOMFY_CANDIDATE_REVISION=profile["vibecomfy_candidate"]["revision"],
               ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST=profile["vibecomfy_candidate"]["source_content_digest"],
               VIBECOMFY_HEADLESS="1")
    if case == "profile_drift":
        env["ASTRID_HOST_READINESS_PROFILE_HASH"] = "sha256:" + "0" * 64
    elif case == "unattested":
        env.pop("ASTRID_VIBECOMFY_ATTESTED_REVISION")
    elif case == "unconfigured":
        env.pop("ASTRID_VIBECOMFY_CHECKOUT")
    elif case == "unsupported_candidate":
        env["ASTRID_VIBECOMFY_CANDIDATE_KIND"] = "ambient"
    elif case == "alias":
        alias = tmp_path / "clone-alias"
        alias.symlink_to(checkout, target_is_directory=True)
        env["ASTRID_VIBECOMFY_CHECKOUT"] = str(alias)
    foreign = tmp_path / "foreign"
    (foreign / "vibecomfy").mkdir(parents=True)
    (foreign / "vibecomfy" / "__init__.py").write_text("raise AssertionError('foreign dependency must never execute')\n")
    if case == "ambient":
        env["PYTHONPATH"] = str(foreign)
    script = r'''
import importlib.util,json,os,sys
from pathlib import Path
from types import ModuleType
source,case,foreign,checkout,product_roots=sys.argv[1:]
# A production installed interpreter has no VibeComfy of its own. Remove any this test interpreter
# carries (an editable install is a .pth path entry or a meta-path finder) so only the attested root can resolve.
sys.path[:]=[p for p in sys.path if not (Path(p)/'vibecomfy'/'__init__.py').is_file()]
sys.meta_path[:]=[f for f in sys.meta_path if 'vibecomfy' not in getattr(f,'__module__','')]
# Ordinary source tests bind their known product roots explicitly. Qualified
# installed tests supply no roots, retaining the interpreter's true -I origins.
sys.path.extend(json.loads(product_roots))
spec=importlib.util.spec_from_file_location('astrid.core.execution.generic_host',source)
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
if case in ('cached_collision','cached_submodule_collision'):
    name='vibecomfy' if case=='cached_collision' else 'vibecomfy.workflow_bundle'
    wrong=ModuleType(name);wrong.__file__=str(Path(foreign)/'vibecomfy/__init__.py')
    sys.modules[name]=wrong
elif case=='resolution_collision':
    sys.path.insert(0,foreign)
before=sys.path[:]
products={name:importlib.util.find_spec(name).origin for name in ('astrid','runtime_protocol')}
try:
    result=module._install_host_vibecomfy_import_root()
except module.HostError:
    assert case not in ('installed','ambient','source','unconfigured')
    assert sys.path==before
    print(json.dumps({'rejected':True,'path_unchanged':True}))
else:
    assert case in ('installed','ambient','source','unconfigured')
    if case in ('installed','ambient'):
        assert str(result)==checkout and sys.path==before+[checkout]
        import vibecomfy.workflow_bundle as bundle
        assert Path(bundle.__file__).resolve().is_relative_to(Path(checkout)/'vibecomfy')
        assert module._install_host_vibecomfy_import_root()==result
        assert sys.path==before+[checkout]
    else:
        assert result is None and sys.path==before
    assert products=={name:importlib.util.find_spec(name).origin for name in products}
    print(json.dumps({'rejected':False,'product_origins_unchanged':True}))
'''
    result = subprocess.run([python, "-I", "-B", "-c", script, host_module.__file__, case,
                             str(foreign), checkout, json.dumps(products)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout.strip().splitlines()[-1])
    assert row["rejected"] == (case not in {"installed", "ambient", "source", "unconfigured"})


class FakeRuntime(_CurrentFakeRuntime):
    def health(self):
        return {**super().health(), "instance_id": "fixture-instance",
                "runtime_instance_id": "fixture-instance", "coordinator_epoch": "fixture-instance"}



@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-state.json"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)


def pack(tmp_path, *, code="def run(value='default'):\n    return value\n", path="actions/hello.py", inputs=None, outputs=None, conditions=None):
    root = tmp_path / "nested repo" / "unrelated"
    source = root / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(code)
    declaration = {
        "description": "An unrelated ordinary action.",
        "invocation": {"kind": "python", "path": path, "function": "run"},
        "inputs": inputs if inputs is not None else [{"name": "value", "type": "string", "required": False, "default": "default"}],
        "outputs": outputs if outputs is not None else {},
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"], "project_scope": "optional"},
    }
    if conditions is not None:
        declaration["conditions"] = conditions
    (root / "pack.yaml").write_text(yaml.safe_dump({"schema_version": 3, "id": "unrelated", "name": "Unrelated", "version": "1.0.0", "actions": {"calculate": declaration}}))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[root], client=runtime)
    records = host.discover()
    assert [r.id for r in records] == ["unrelated.calculate"]
    definition, admission = host.admit(capability_kind="executor", capability_id="unrelated.calculate")
    return root, host, runtime, definition, admission


def _legacy_probe_manifest(tmp_path, names, *, fixed=None, required=()):
    manifest = _write_manifest(tmp_path / 'legacy-probe')
    data = json.loads(manifest.read_text())
    data['isolation'] = {'mode': 'subprocess', 'env_passthrough': list(names)}
    data['command']['env'] = fixed or {}
    data['metadata']['required_env'] = list(required)
    manifest.write_text(json.dumps(data))
    return manifest


@pytest.mark.parametrize('credential,declared', [
    ('HIVEMIND_CONTRIBUTOR_KEY', True), ('HIVEMIND_ANON_KEY', False),
])
def test_startup_probes_omit_legacy_credentials_without_declaring_them(tmp_path, credential, declared):
    from astrid.core.execution.generic_host import _launch_worker_probe_environments
    from astrid.core.execution.executor.folder import load_folder_executor
    manifest = _legacy_probe_manifest(
        tmp_path, ['HIVEMIND_API_URL', credential],
        required=[credential] if declared else [])
    # The real legacy definition carries passthrough, not secrets_required.
    definition = load_folder_executor(manifest.parent)
    assert definition.isolation.env_passthrough == ('HIVEMIND_API_URL', credential)
    assert definition.isolation.secrets_required == ()
    environment = {'PATH': '/usr/bin', 'HIVEMIND_API_URL': 'https://fixture.invalid',
                   credential: 'credential-sentinel-never-export',
                   'UNDECLARED_TOKEN': 'ambient-sentinel-never-export'}
    variants = _launch_worker_probe_environments((tmp_path,), environment)
    assert len(variants) == 3  # Safe baseline, actual native and command projections.
    assert 'HIVEMIND_API_URL' not in variants[0]
    for variant in variants[1:]:
        assert variant['HIVEMIND_API_URL'] == environment['HIVEMIND_API_URL']
        assert variant['PYTHONPATH']
    for variant in variants:
        assert credential not in variant and 'UNDECLARED_TOKEN' not in variant
        assert not any('sentinel-never-export' in value for value in variant.values())


@pytest.mark.parametrize('invalid', ['9BAD_TOKEN', 'BROKEN-PUBLIC'])
def test_startup_probe_omission_preserves_environment_name_validation(tmp_path, monkeypatch, invalid):
    from dataclasses import replace
    from astrid.core.execution import generic_host
    from astrid.core.execution.executor import folder
    from astrid.core.subprocess_env import SubprocessEnvPolicyError
    _legacy_probe_manifest(tmp_path, ['PUBLIC_VALUE'])
    original = folder.load_folder_executor

    def loaded_with_invalid_projection(root):
        definition = original(root)
        return replace(definition, isolation=replace(definition.isolation, env_passthrough=(invalid,)))

    monkeypatch.setattr(folder, 'load_folder_executor', loaded_with_invalid_projection)
    with pytest.raises(SubprocessEnvPolicyError, match='valid environment variable name'):
        generic_host._launch_worker_probe_environments((tmp_path,), {'PATH': '/usr/bin'})


def test_startup_probes_keep_explicit_undeclared_credential_rejection(tmp_path):
    from astrid.core.execution.generic_host import _launch_worker_probe_environments
    _legacy_probe_manifest(tmp_path, ['HIVEMIND_ANON_KEY'], fixed={'UNDECLARED_TOKEN': 'fixture'})
    with pytest.raises(HostError, match='undeclared secret environment variable'):
        _launch_worker_probe_environments((tmp_path,), {'PATH': '/usr/bin'})


def invoke(tmp_path, host, definition, admission, *, inputs=None, dry_run=False, cancelled=None):
    attempt = tmp_path / "attempt"
    return host.invoke_capability(capability_kind="executor", capability_id="unrelated.calculate",
                                  request={"inputs": inputs or {}, "out": str(attempt / "outputs"),
                                           "project": "demo", "project_was_auto_resolved": True,
                                           "python_exec": sys.executable, "verbose": True,
                                           "dry_run": dry_run, "invocation": "runtime"},
                                  attempt=attempt, definition=definition, admission=admission,
                                  cancelled=cancelled)


def test_static_discover_admit_dry_run_and_owned_child(tmp_path):
    sentinel = tmp_path / "imported"
    root, host, runtime, definition, admission = pack(tmp_path, code=f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('entry')\ndef run(value):\n    return {{'value': value, 'pid': __import__('os').getpid()}}\n")
    (root / "__init__.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('initializer')\n")
    # Refresh after deliberate initializer addition, which invalidates admission.
    host.discover()
    definition, admission = host.admit(capability_kind="executor", capability_id="unrelated.calculate")
    assert not sentinel.exists()
    assert admission["source_roots"] == [str(root)] and admission["python_path_roots"] == []
    assert invoke(tmp_path, host, definition, admission, dry_run=True).ok
    assert not sentinel.exists()
    result = invoke(tmp_path, host, definition, admission)
    assert result.payload["action_result"]["value"] == "default"
    assert result.payload["action_result"]["pid"] != os.getpid()
    assert result.outputs == [] and sentinel.exists()
    host.register()
    assert runtime.registrations[0][1]["resource_keys"] == ["cpu"]


def test_python_action_receives_original_host_bridge_before_import(tmp_path):
    """Exercise the host's real FD/argv boundary, without fixture installation."""
    code = """from astrid.sdk import _child_bridge
import os
assert _child_bridge._bridge is not None
assert not os.get_inheritable(_child_bridge._bridge._channel.fileno())
def run(value):
    return value
"""
    _, host, _, definition, admission = pack(tmp_path, code=code)
    parent, inherited = socket.socketpair()
    launched = []

    class OriginalBridge:
        def inherited_fd(self):
            return inherited.fileno()

        def launched(self):
            launched.append(True)

    try:
        attempt = tmp_path / "attempt"
        result = host.invoke_capability(
            capability_kind="executor", capability_id="unrelated.calculate",
            request={"inputs": {}, "out": str(attempt / "outputs"),
                     "project": "demo", "project_was_auto_resolved": True,
                     "python_exec": sys.executable, "invocation": "runtime"},
            attempt=attempt, definition=definition, admission=admission,
            child_bridge=OriginalBridge())
        assert result.ok and result.payload["action_result"] == "default"
        assert launched == [True]
        inherited.close()
        parent.settimeout(2)
        assert parent.recv(1) == b""  # Worker closed the original stream.
    finally:
        inherited.close()
        parent.close()


@pytest.mark.parametrize("form", ["-c", "-m", "script"])
def test_private_child_command_preserves_entrypoint_and_original_bridge(tmp_path, form):
    code = """from astrid.sdk import _child_bridge
import os, sys, __main__
assert _child_bridge._bridge is not None
assert not os.get_inheritable(_child_bridge._bridge._channel.fileno())
assert sys.argv[1:] == ['bound-argument']
main_value = 42
assert __main__.main_value == 42
print('original-bridge-bound')
"""
    script = tmp_path / "private_probe.py"
    script.write_text(code)
    args = [form, code if form == "-c" else "private_probe", "bound-argument"]
    if form == "script":
        args = [str(script), "bound-argument"]
    parent, inherited = socket.socketpair()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[3]), str(tmp_path))))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "astrid.core.execution.generic_host_worker",
             "--child-command", str(inherited.fileno()), json.dumps(args)],
            cwd=tmp_path, env=env, pass_fds=(inherited.fileno(),),
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "original-bridge-bound"
        inherited.close()
        parent.settimeout(2)
        assert parent.recv(1) == b""
    finally:
        inherited.close()
        parent.close()


@pytest.mark.parametrize("args", [[], ["-c"], ["-m"], ["-I", "work.py"], [7], {"command": "work.py"}])
def test_private_child_command_rejects_invalid_controls_before_bridge_install(monkeypatch, args):
    from astrid.core.execution import generic_host_worker as worker
    def forbidden(fd):
        pytest.fail("malformed controls reached inherited authority")
    monkeypatch.setattr(worker, "_install_inherited_bridge", forbidden)
    with pytest.raises(ValueError):
        worker._run_child_command("invalid-fd", json.dumps(args))


def test_inherited_bridge_rejects_absent_original_descriptor():
    from astrid.core.execution import generic_host_worker as worker
    with pytest.raises(OSError):
        worker._install_inherited_bridge("2147483647")


@pytest.mark.parametrize("value", [None, False, 0, "", [], [1, False], {"ok": False, "outputs": [{"path": "/fake"}], "returncode": 99}])
def test_strict_json_results_are_nested_data(tmp_path, value):
    _, host, _, definition, admission = pack(tmp_path, code=f"def run():\n    return {value!r}\n", inputs=[])
    result = invoke(tmp_path, host, definition, admission)
    assert result.ok and result.returncode == 0 and result.outputs == []
    assert result.payload["action_result"] == value


@pytest.mark.parametrize("expression", ["float('nan')", "float('inf')", "object()", "(1, 2)", "{1: 'bad'}", "__import__('pathlib').Path('/fake')"])
def test_non_json_result_fails(tmp_path, expression):
    _, host, runtime, definition, admission = pack(tmp_path, code=f"def run():\n    return {expression}\n", inputs=[])
    with pytest.raises(HostError, match="strict JSON"):
        invoke(tmp_path, host, definition, admission)
    assert runtime.settlements == []


def test_output_schema_validates_return_without_file_ports(tmp_path):
    _, host, _, definition, admission = pack(tmp_path, code="def run(value):\n    return {'answer': 42}\n", outputs={"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]})
    assert definition["outputs"] == []
    assert invoke(tmp_path, host, definition, admission).payload["action_result"] == {"answer": 42}
    definition["metadata"]["action_outputs_schema"]["properties"]["answer"]["type"] = "string"
    from astrid.core.execution.generic_host import _capability_digest
    admission["capability_digest"] = _capability_digest(definition)
    with pytest.raises(HostError, match="answer"):
        invoke(tmp_path, host, definition, admission)


def test_input_schema_before_import_and_only_effective_kwargs(tmp_path):
    sentinel = tmp_path / "imported"
    schema = {"type": "object", "properties": {"value": {"type": "integer", "minimum": 1, "default": 3}}, "additionalProperties": False}
    _, host, _, definition, admission = pack(tmp_path, code=f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\ndef run(**kwargs):\n    return kwargs\n", inputs=schema)
    with pytest.raises(HostError, match="minimum"):
        invoke(tmp_path, host, definition, admission, inputs={"value": 0})
    assert not sentinel.exists()
    assert invoke(tmp_path, host, definition, admission).payload["action_result"] == {"value": 3}


@pytest.mark.parametrize("path, imports", [("actions/hello.py", "from .helper import value\nfrom ..shared import extra\n"), ("actions/larger/run.py", "from ..helper import value\nfrom ...shared import extra\n")])
def test_relative_helpers_initializers_and_source_only_bytecode(tmp_path, path, imports):
    root, host, _, _, _ = pack(tmp_path, path=path, code=imports+"import json\ndef run(value=None):\n    from . import initialized\n    return [value, extra, initialized, json.loads('true')]\n")
    (root / "actions/helper.py").write_text("value = 'cached'\n")
    original_stat = (root / "actions/helper.py").stat()
    py_compile.compile(str(root / "actions/helper.py"), doraise=True)
    (root / "actions/helper.py").write_text("value = 'source'\n")
    os.utime(root / "actions/helper.py", ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    (root / "shared.py").write_text("extra = 'shared'\n")
    (root / Path(path).parent / "__init__.py").write_text("initialized = 'initializer'\n")
    # Function parameter shadows imported value; alias through initializer to prove helper source.
    (root / path).write_text(imports+"import json\ndef run(**kwargs):\n    from . import initialized\n    return [value, extra, initialized, json.loads('true')]\n")
    host.discover()
    definition, admission = host.admit(capability_kind="executor", capability_id="unrelated.calculate")
    assert invoke(tmp_path, host, definition, admission).payload["action_result"] == ["source", "shared", "initializer", True]


@pytest.mark.parametrize("target", ["actions/hello.py", "actions/helper.py", "__init__.py", "pack.yaml", "contract.json"])
def test_whole_pack_mutation_invalidates_admission(tmp_path, target):
    root, host, _, _, _ = pack(tmp_path)
    for name in ["actions/helper.py", "__init__.py", "contract.json"]:
        (root / name).write_text("{}" if name.endswith('.json') else "# admitted\n")
    host.discover()
    definition, admission = host.admit(capability_kind="executor", capability_id="unrelated.calculate")
    (root / target).write_text((root / target).read_text() + "\n# changed\n")
    with pytest.raises(HostError, match="source digest changed"):
        invoke(tmp_path, host, definition, admission)


@pytest.mark.parametrize("tamper", ["root", "source_roots", "ancestor", "digest", "missing"])
def test_admission_identity_is_required(tmp_path, tamper):
    root, host, _, definition, admission = pack(tmp_path)
    if tamper == "root":
        definition["metadata"]["pack_root"] = str(root.parent)
    elif tamper == "source_roots":
        admission["source_roots"] = [str(root / "actions")]
    elif tamper == "ancestor":
        admission["python_path_roots"] = [str(root.parent)]
    elif tamper == "digest":
        admission["capability_digest"] = "bad"
    else:
        admission = {}
    with pytest.raises(HostError, match="admission|admitted|namespace"):
        invoke(tmp_path, host, definition, admission)


@pytest.mark.parametrize("path,function", [("/tmp/escape.py", "run"), ("actions/../escape.py", "run"), ("actions/hello.py", "bad-name")])
def test_malformed_entrypoint_rejected_without_import(tmp_path, path, function):
    _, host, _, definition, admission = pack(tmp_path)
    definition["metadata"]["action_invocation"].update(path=path, function=function)
    from astrid.core.execution.generic_host import _capability_digest
    admission["capability_digest"] = _capability_digest(definition)
    with pytest.raises(HostError, match="invalid Python action"):
        invoke(tmp_path, host, definition, admission)


def test_symlink_escape_rejected(tmp_path):
    root, host, _, definition, admission = pack(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("raise AssertionError('escape imported')\n")
    (root / "actions/hello.py").unlink()
    (root / "actions/hello.py").symlink_to(outside)
    assert host.discover() == ()
    with pytest.raises(HostError, match="source digest changed"):
        invoke(tmp_path, host, definition, admission)


@pytest.mark.parametrize("code,error", [("other = 1\n", "missing or not callable"), ("run = 1\n", "missing or not callable"), ("def run(value):\n    raise RuntimeError('intentional failure')\n", "intentional failure")])
def test_action_failure_uses_existing_host_error(tmp_path, code, error):
    _, host, runtime, definition, admission = pack(tmp_path, code=code)
    with pytest.raises(HostError, match=error):
        invoke(tmp_path, host, definition, admission)
    assert runtime.settlements == []


def test_condition_skip_and_public_runner_bypass_are_import_free(tmp_path, monkeypatch):
    _, host, _, definition, admission = pack(tmp_path, code="raise AssertionError('must not import')\n", conditions=[{"kind": "skip_if_input", "input": "value"}])
    assert invoke(tmp_path, host, definition, admission).ok
    definition["conditions"] = []
    from astrid.core.execution.executor.schema import validate_executor_definition
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    request = ExecutorRunRequest(executor_id="unrelated.calculate", out=tmp_path / "out", project="demo", project_was_auto_resolved=True)
    with pytest.raises(ValueError, match="admitted internal host worker"):
        run_executor(request, ExecutorRegistry([validate_executor_definition(definition)]))


def test_command_action_and_v2_command_harvest(tmp_path):
    root, _, _, _, _ = pack(tmp_path)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    action = manifest["actions"]["calculate"]
    action["invocation"] = {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])", "{out}/answer.txt", "{value}"]}}
    action["outputs"] = [{"name": "answer", "type": "file", "mode": "create", "path_template": "{out}/answer.txt", "artifact_type": "text/plain"}]
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    _write_manifest(tmp_path / "legacy")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[root, tmp_path / "legacy"], client=runtime)
    host.discover()
    assert set(host.capabilities) == {"unrelated.calculate", "test.echo"}
    host.register()
    for index, capability in enumerate(["unrelated.calculate", "test.echo"]):
        task = {"task": {"id": f"task-{index}", "capability": capability, "project_id": "demo", "attempt_id": f"attempt-{index}", "fence": 1, "spec": {"spec": {"inputs": {"value": "argv with spaces"} if index == 0 else {}}}}}
        runtime.tasks[f"task-{index}"] = task
        assert host.run_task(task, lease_token="lease")["task"]["status"] == "completed"
    assert len(runtime.settlements) == 2
    assert any(obj["data"] == b"argv with spaces" for obj in runtime.uploaded_objects.values())


def test_cancelled_python_action_cannot_return_success(tmp_path):
    _, host, runtime, definition, admission = pack(tmp_path, code="import time\ndef run(value):\n    time.sleep(10)\n    return value\n")
    with pytest.raises(HostCancelled):
        invoke(tmp_path, host, definition, admission, cancelled=lambda: True)
    assert runtime.settlements == []


@pytest.mark.parametrize("shape,state,pump_only", [
    (shape, state, False)
    for shape in ("typed", "canonical", "wrapped", "legacy")
    for state in ("cancel_requested", "cancelled")
] + [("canonical", "cancel_requested", True)])
def test_runtime_cancellation_contains_owned_action_without_terminal_ack(tmp_path, monkeypatch, shape, state, pump_only):
    from types import SimpleNamespace
    import threading
    from astrid.core.execution.process_group import verify_tree_absent

    entered = tmp_path / "entered"
    _, host, runtime, _, _ = pack(tmp_path, code=(
        "from pathlib import Path\nimport time\n"
        f"def run(value):\n    Path({str(entered)!r}).write_text('entered')\n"
        "    time.sleep(30)\n    return value\n"))
    host.register()
    admitted = task_for(runtime)
    original_task = runtime.tasks["python-task"]
    observed = []

    def current_task(task_id):
        assert task_id == "python-task"
        pump = threading.current_thread().name.startswith("astrid-heartbeat-")
        current_state = state if entered.exists() and (not pump_only or pump) else "running"
        observed.append(current_state)
        if shape == "typed":
            return SimpleNamespace(state=current_state)
        if shape == "legacy":
            return {"task": {"status": current_state}}
        # Canonical state wins over a contradictory legacy field.
        value = {"state": current_state, "status": "running"}
        return {"task": value} if shape == "wrapped" else value

    monkeypatch.setattr(runtime, "task", current_task)
    owned = []
    track = host._track_process

    def track_original(process):
        owned.append(process)
        track(process)

    monkeypatch.setattr(host, "_track_process", track_original)
    result = host.run_task(admitted, lease_token="lease")
    assert result["cancelled"] is True and state in observed
    assert entered.read_text() == "entered" and len(owned) == 1
    assert owned[0].returncode is not None
    from astrid.core.execution import task_scope as _ts; assert _ts.stop(owned[0], 0.1).status == "settled"
    assert not host._active_processes and not host._unresolved_task_scopes
    assert runtime.settlements == [] and runtime.failures == [] and runtime.uploaded_objects == {}
    # Local containment neither fabricates a Runtime terminal transition nor
    # changes the original admitted task/attempt identity.
    assert runtime.tasks["python-task"] is original_task


@pytest.mark.parametrize("wrapped", [False, True])
def test_canonical_running_state_wins_over_legacy_cancelled_field(tmp_path, monkeypatch, wrapped):
    _, host, runtime, _, _ = pack(tmp_path)
    host.register()
    admitted = task_for(runtime)
    current = {"state": "running", "status": "cancelled"}
    monkeypatch.setattr(runtime, "task", lambda task_id: {"task": current} if wrapped else current)
    assert host.run_task(admitted, lease_token="lease")["task"]["status"] == "completed"
    assert len(runtime.settlements) == 1 and runtime.failures == []


@pytest.mark.parametrize("path", ["actions/hello-world.py", "actions/a-folder/script.v1.py"])
def test_canonical_punctuation_paths_use_opaque_module_names(tmp_path, path):
    _, host, _, definition, admission = pack(tmp_path, path=path)
    assert invoke(tmp_path, host, definition, admission).payload["action_result"] == "default"


def task_for(runtime, capability="unrelated.calculate"):
    task = {"task": {"id": "python-task", "capability": capability, "project_id": "demo", "attempt_id": "python-attempt", "fence": 1, "spec": {"spec": {"inputs": {}}}}}
    runtime.tasks["python-task"] = task
    return task


def test_python_return_settles_with_trusted_evidence_and_no_files(tmp_path):
    _, host, runtime, _, _ = pack(tmp_path, code="def run(value):\n    return {'ok': False, 'returncode': 99, 'outputs': ['/fake']}\n")
    host.register()
    assert host.run_task(task_for(runtime), lease_token="lease")["task"]["status"] == "completed"
    receipt = runtime.settlements[0][2]
    assert receipt["outputs"] == []
    assert receipt["result"]["action_result"] == {"ok": False, "returncode": 99, "outputs": ["/fake"]}
    assert receipt["result"]["process_evidence"]["returncode"] == 0
    assert receipt["result"]["process_evidence"]["process_id"] != os.getpid()
    assert runtime.uploaded_objects == {}


@pytest.mark.parametrize("creates_file", [False, True])
def test_python_declared_files_require_real_attempt_output(tmp_path, creates_file):
    code = "def run(value):\n"
    if creates_file:
        code += "    from pathlib import Path\n    Path('outputs').mkdir(exist_ok=True)\n    Path('outputs/answer.txt').write_text(value)\n"
    code += "    return {'outputs': [{'path': '/fake'}]}\n"
    _, host, runtime, _, _ = pack(tmp_path, code=code, outputs=[{"name": "answer", "type": "file", "mode": "create", "path_template": "{out}/answer.txt", "artifact_type": "text/plain"}])
    host.register()
    if creates_file:
        assert host.run_task(task_for(runtime), lease_token="lease")["task"]["status"] == "completed"
        assert next(iter(runtime.uploaded_objects.values()))["data"] == b"default"
    else:
        with pytest.raises(HostError, match="output"):
            host.run_task(task_for(runtime), lease_token="lease")
        assert runtime.settlements == [] and runtime.uploaded_objects == {}


def test_private_imports_add_no_repository_or_pack_pythonpath(tmp_path):
    root, host, _, definition, admission = pack(tmp_path, code="import os, sys\ndef run(value):\n    return {'sys_path': sys.path, 'pythonpath': os.environ['PYTHONPATH']}\n")
    returned = invoke(tmp_path, host, definition, admission).payload["action_result"]
    assert str(root) not in returned["sys_path"] and str(root.parent) not in returned["sys_path"]
    assert str(root) not in returned["pythonpath"].split(os.pathsep)
    assert str(root.parent) not in returned["pythonpath"].split(os.pathsep)


def test_legacy_child_command_and_ancestor_import_fence(tmp_path):
    _write_manifest(tmp_path / "legacy")
    host = GenericPackHost(pack_roots=[tmp_path / "legacy"])
    definition, admission = host.admit("executor", "test.echo")
    attempt = tmp_path / "legacy-attempt"
    request = {"inputs": {}, "out": str(attempt / "outputs"), "project": "demo", "project_was_auto_resolved": True, "python_exec": sys.executable, "invocation": "runtime"}
    escaped = dict(admission, python_path_roots=[str(tmp_path)])
    with pytest.raises(HostError, match="outside the source fence"):
        host.invoke_capability(capability_kind="executor", capability_id="test.echo", request=request, attempt=attempt, definition=definition, admission=escaped)
    (attempt / "outputs").mkdir(parents=True, exist_ok=True)
    result = host.invoke_capability(capability_kind="executor", capability_id="test.echo", request=request, attempt=attempt, definition=definition, admission=admission)
    assert result.ok and result.outputs[0]["name"] == "answer"


@pytest.mark.parametrize("value", [None, ""])
def test_schema_required_value_preserves_json_semantics(tmp_path, value):
    schema = {"type": "object", "properties": {"value": {"type": ["null", "string"]}}, "required": ["value"]}
    _, host, _, definition, admission = pack(tmp_path, inputs=schema)
    assert invoke(tmp_path, host, definition, admission, inputs={"value": value}).payload["action_result"] == value


def test_cancellation_after_child_result_blocks_success(tmp_path):
    _, host, runtime, definition, admission = pack(tmp_path)
    result_path = tmp_path / "attempt/.astrid-capability-result.json"
    observed = []
    def cancelled():
        if result_path.exists():
            observed.append(json.loads(result_path.read_text()))
            return True
        return False
    with pytest.raises(HostCancelled):
        invoke(tmp_path, host, definition, admission, cancelled=cancelled)
    assert observed[-1]["ok"] is True
    assert runtime.settlements == [] and runtime.uploaded_objects == {}


def test_legacy_builtin_pipeline_selection_is_preserved(tmp_path, monkeypatch):
    from astrid.core.execution.executor import runner
    from astrid.core.execution.executor.schema import validate_executor_definition
    executor = validate_executor_definition({"id": "legacy.pipeline", "name": "Legacy", "kind": "built_in", "version": "1.0.0", "metadata": {"pipeline_step": "existing-step"}})
    request = ExecutorRunRequest(executor_id=executor.id, out=tmp_path, project="demo")
    expected = runner.ExecutorRunResult(executor_id=executor.id, kind="built_in", payload={"legacy": True})
    calls = []
    def builtin(definition, request):
        calls.append((definition.id, request.executor_id))
        return expected
    monkeypatch.setattr(runner, "_run_builtin_executor", builtin)
    assert runner._run_executor_inner(request, executor) is expected
    assert calls == [(executor.id, executor.id)]


def test_legacy_orchestrator_child_command_route(tmp_path):
    source = tmp_path / "legacy_orchestrator"
    source.mkdir()
    (source / "orchestrator.yaml").write_text(yaml.safe_dump({"id": "legacy.compose", "name": "Legacy Compose", "kind": "external", "version": "1.0.0", "runtime": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "pass"]}}, "inputs": [], "outputs": []}))
    host = GenericPackHost(pack_roots=[source])
    definition, admission = host.admit("orchestrator", "legacy.compose")
    attempt = tmp_path / "orchestrator-attempt"
    result = host.invoke_capability(capability_kind="orchestrator", capability_id="legacy.compose", request={"inputs": {}, "out": str(attempt / "outputs"), "project": "demo", "project_was_auto_resolved": True, "python_exec": sys.executable, "invocation": "runtime"}, attempt=attempt, definition=definition, admission=admission)
    assert result.ok and result.returncode == 0


def test_public_sdk_and_host_share_exact_definition_admission(tmp_path):
    import astrid
    from astrid.core.execution.generic_host import _capability_digest
    root, _, _, definition, admission = pack(tmp_path)
    capability = astrid.get_capability("unrelated.calculate", kind="action", extra_pack_roots=(str(root),))
    assert capability.definition == definition
    assert _capability_digest(capability.definition) == admission["capability_digest"]


@pytest.mark.parametrize("previous_headless", [None, "host-original"])
@pytest.mark.parametrize("loader_fails", [False, True])
def test_host_identity_uses_shared_canonical_bundle_staging(tmp_path, monkeypatch, previous_headless, loader_fails):
    import hashlib
    import inspect
    from types import SimpleNamespace

    from astrid.core.execution.generic_host import _prepare_vibecomfy_execution_identity
    from astrid.packs.vibecomfy import production_engine

    if previous_headless is None:
        monkeypatch.delenv("VIBECOMFY_HEADLESS", raising=False)
    else:
        monkeypatch.setenv("VIBECOMFY_HEADLESS", previous_headless)
    expected_bytes = {
        "workflow.py": b"# synthetic canonical Python member\n",
        "workflow.vibe.json": b'{"identity":"canonical-fixture","revision":"revision-1"}\n',
        "source.json": b'{"source":"synthetic fixture"}\n',
    }
    inputs = {}
    for port, basename in zip(("python", "companion", "source"), expected_bytes):
        member = tmp_path / f"materialized-{port}.bin"
        member.write_bytes(expected_bytes[basename])
        inputs[port] = str(member)
    scratch = tmp_path / "attempt" / "identity"
    profile = {"verified_facts": {"exact": {"model_digest": "model-content-digest"}}}
    loaded = production_engine.LoadedWorkflow(
        resolved=SimpleNamespace(workflow=SimpleNamespace(
            requirements={"runtime": {"comfy_version": "fixture-version"}},
            metadata={"model_assets": ["synthetic-model"], "prompt": "excluded"},
        )),
        model_id="synthetic-model", template_id="synthetic-template",
        workflow_identity="canonical-fixture", workflow_revision="revision-1",
        workflow_content_digest="canonical-content-digest",
    )
    staged = []

    def load_boundary(workflow_path, loader_scratch, comfy_url=""):
        helper = inspect.currentframe().f_back.f_locals["staged_workflow_path"]
        assert helper.__module__ == "astrid.packs.vibecomfy.shared.bundle_inputs"
        assert Path(inspect.getsourcefile(helper.__wrapped__)).name == "bundle_inputs.py"
        assert os.environ["VIBECOMFY_HEADLESS"] == "1"
        assert workflow_path.name == "workflow.py"
        assert workflow_path.parent.parent == scratch
        assert workflow_path.parent.name.startswith("astrid-vibecomfy-bundle-")
        assert loader_scratch == scratch / "canonical-loader"
        assert {p.name: p.read_bytes() for p in workflow_path.parent.iterdir()} == expected_bytes
        staged.append(workflow_path.parent)
        if loader_fails:
            raise production_engine.ProductionEngineError("synthetic loader failure")
        return loaded

    # Only canonical loading is mocked; staging and both identity functions are real.
    monkeypatch.setattr(production_engine, "load_workflow_path", load_boundary)
    before = (os.environ.copy(), sys.path[:], sys.meta_path[:], Path.cwd())
    if loader_fails:
        with pytest.raises(HostError, match="canonical input preflight failed: synthetic loader failure") as error:
            _prepare_vibecomfy_execution_identity(inputs, scratch, profile)
        assert isinstance(error.value.__cause__, production_engine.ProductionEngineError)
    else:
        expected_identity = hashlib.sha256(json.dumps({
            "model_id": "synthetic-model", "template_id": "synthetic-template",
            "model_digest": "model-content-digest", "workflow_identity": "canonical-fixture",
            "workflow_revision": "revision-1", "workflow_content_digest": "canonical-content-digest",
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert _prepare_vibecomfy_execution_identity(inputs, scratch, profile) == (
            expected_identity, "synthetic-model", "synthetic-template", {
                "model_id": "synthetic-model",
                "requirements": {"runtime": {"comfy_version": "fixture-version"}},
                "resident_metadata": {"model_assets": ["synthetic-model"]},
            },
        )
    assert len(staged) == 1 and not staged[0].exists()
    assert list(scratch.iterdir()) == []
    assert {basename: Path(inputs[port]).read_bytes()
            for port, basename in zip(("python", "companion", "source"), expected_bytes)} == expected_bytes
    assert (os.environ.copy(), sys.path[:], sys.meta_path[:], Path.cwd()) == before


@pytest.mark.parametrize('cleanup_failure',[False,True])
def test_host_only_cleanup_ack_requires_actual_child_owner_shutdown(tmp_path,monkeypatch,cleanup_failure):
    from types import SimpleNamespace
    from astrid.core.execution import generic_host as module
    from astrid.core.execution import custody_broker
    preparation=module.LocalExecutionPreparation(operation_id='op',channel_id='ch')
    profile={'execution_mode':'host_only_cpu','profile_digest':'sha256:'+'1'*64}
    preparation._request={'owner_epoch':'epoch','profile':profile,'custody_scope':str(tmp_path)}
    preparation._retained_relay_actor=SimpleNamespace(verify=lambda:{'pid':123,'birth_id':'owner'})
    calls=[]
    def shutdown():
        calls.append('shutdown')
        if cleanup_failure: raise module.CleanupUncertainError('owned child unresolved')
    preparation._claim_host=SimpleNamespace(shutdown=shutdown)
    ref={'target':{'pid':os.getpid(),'birth_id':module.process_birth_identity()}}
    class Authority:
        def __init__(self,*args):pass
        def verify_reference(self,*args,**kwargs):calls.append('verify')
    monkeypatch.setattr(custody_broker,'RoleCustodyAuthority',Authority)
    request={'version':module._LOCAL_PREPARATION_VERSION,'command':'abort_local_execution','operation_id':'op','channel_id':'ch',
        'owner_epoch':'epoch','profile_digest':profile['profile_digest'],'profile_binding_digest':module._handoff_digest(profile),
        'custody_scope':str(tmp_path),'custody_capabilities':{'host':ref}}
    result=preparation.dispatch(request)
    assert calls==['verify','shutdown']
    assert result['status']==('unresolved' if cleanup_failure else 'cleaned')
    assert preparation._aborted is (not cleanup_failure)
    if cleanup_failure: assert 'cleanup' not in result
    else: assert result['cleanup']=={'host_children':'absent'}


def test_host_only_cleanup_wrong_owner_binding_never_calls_shutdown(tmp_path):
    from types import SimpleNamespace
    from astrid.core.execution import generic_host as module
    preparation=module.LocalExecutionPreparation(operation_id='op',channel_id='ch')
    preparation._request={'owner_epoch':'epoch','profile':{'execution_mode':'host_only_cpu'},'custody_scope':str(tmp_path)}
    preparation._claim_host=SimpleNamespace(shutdown=lambda:pytest.fail('foreign cleanup'))
    with pytest.raises(module.HostError,match='binding'):
        preparation.dispatch({'command':'abort_local_execution','version':module._LOCAL_PREPARATION_VERSION,
            'operation_id':'other','channel_id':'ch','owner_epoch':'epoch'})


@pytest.mark.parametrize('boot_case', ['pretty', 'compact', 'raw_digest', 'wrong_semantic', 'malformed_pin', 'corrupt', 'outside', 'symlink', 'symlink_parent', 'wrong_peer'])
def test_host_only_private_prepare_never_allocates_engine_and_rejects_replay(tmp_path,monkeypatch,boot_case):
    from types import SimpleNamespace
    from astrid.core.execution import generic_host as module
    owner={'pid':123,'uid':os.getuid(),'birth_id':'owner','runtime_instance_id':'epoch','coordinator_epoch':'epoch'}
    preparation=module.LocalExecutionPreparation(operation_id='op',channel_id='ch',
        graph_factory=lambda **kwargs:pytest.fail('host-only allocated an engine'))
    preparation._retained_relay_actor=SimpleNamespace(verify=lambda:owner)
    from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash
    import json
    executable=Path(sys.executable);support=tmp_path/'support';support.mkdir()
    boot=support/'astrid-host/boot-manifest.json';boot.parent.mkdir()
    manifest=build_manifest(registry={},fixtures=(),profile_order=())
    boot.write_text(json.dumps(manifest,indent=2)+'\n')
    semantic=load_boot_manifest_hash(boot,support_root=support)
    raw=module.hashlib.sha256(boot.read_bytes()).hexdigest()
    assert semantic != raw  # Pretty file bytes are independently pinned, not the manifest stamp.
    selected='sha256:'+semantic
    if boot_case=='compact':boot.write_text(json.dumps(manifest,separators=(',',':')))
    elif boot_case=='raw_digest':selected='sha256:'+raw
    elif boot_case=='wrong_semantic':selected='sha256:'+'0'*64
    elif boot_case=='malformed_pin':selected='sha256:invalid'
    elif boot_case=='corrupt':boot.write_text('{')
    elif boot_case=='outside':
        outside=tmp_path/'outside/boot-manifest.json';outside.parent.mkdir();outside.write_bytes(boot.read_bytes());boot=outside
    elif boot_case=='symlink':
        target=tmp_path/'saved-boot.json';target.write_bytes(boot.read_bytes());boot.unlink();boot.symlink_to(target)
    elif boot_case=='symlink_parent':
        linked=support/'linked';linked.symlink_to(boot.parent,target_is_directory=True);boot=linked/'boot-manifest.json'
    elif boot_case=='wrong_peer':
        preparation._retained_relay_actor=SimpleNamespace(verify=lambda:{**owner,'birth_id':'another-owner'})
    launch={'source_checkout':str(tmp_path),'pack_root':str(tmp_path/'packs'), 'boot_manifest_path':str(boot),
        'boot_manifest_hash':selected,'capability_matrix':None,'environment':{},'max_concurrency':4}
    profile={'execution_mode':'host_only_cpu','profile_id':'fixture','workspace_uuid':'realm','realm_root':str(tmp_path/'realm'),
        'support_root':str(support),'machine_id':'machine','host_executable':str(executable),'host_artifact_digest':'sha256:'+module.hashlib.sha256(executable.read_bytes()).hexdigest(),
        'session_config_digest':module._handoff_digest(launch),'profile_revision':'revision','profile_digest':'sha256:'+'1'*64,'release_digest':'sha256:'+'2'*64,
        'host_protocol':{'version':module.CURRENT_ROLES_VERSION,'module':'astrid.core.execution.generic_host','source_closure_digest':'3'*64},'host_launch':launch}
    monkeypatch.setattr(module,'source_checkout_closure_digest',lambda path:'3'*64)
    request={'version':module._LOCAL_PREPARATION_VERSION,'command':'prepare_local_execution','operation_id':'op','channel_id':'ch',
        'owner_epoch':'epoch','runtime_owner':owner,'profile':profile,'custody_scope':str(tmp_path/'scope')}
    if boot_case not in ('pretty','compact'):
        expected='original Runtime peer' if boot_case=='wrong_peer' else 'boot input differs'
        with pytest.raises(module.HostError,match=expected):preparation.dispatch(request)
        assert preparation.graph is None and preparation._request is None
        assert preparation._handoff_lock_fd is None
        return
    result=preparation.dispatch(request)
    assert result['execution_mode']=='host_only_cpu' and set(result['processes'])=={'host'}
    assert preparation.graph is None
    assert preparation.dispatch(request)==result
    changed={**request,'profile':{**profile,'profile_revision':'changed'}}
    with pytest.raises(module.HostError,match='replay'):
        preparation.dispatch(changed)
    if preparation._handoff_lock_fd is not None:os.close(preparation._handoff_lock_fd)


@pytest.mark.parametrize('keep_attempt', [False, True])
@pytest.mark.parametrize('drift', [False, True])
def test_managed_review_cancel_retires_only_observed_owner_and_preserves_drift(tmp_path, keep_attempt, drift):
    """Real inherited command/HTTP receiver/owned termination, no task transport claim."""
    import time
    import urllib.request
    import urllib.error
    from astrid.core.execution import generic_host as module
    root = Path(__file__).resolve().parents[3]/'astrid/packs/editorial'
    host = GenericPackHost(pack_roots=[root], client=FakeRuntime())
    host.discover()
    _, admission = host.admit(capability_kind='executor', capability_id='editorial.human_review')
    record = host.capabilities['editorial.human_review']
    attempt = tmp_path.resolve()/'review-attempt'; (attempt/'outputs').mkdir(parents=True)
    (attempt/'inputs.html').write_text('<html>review</html>'); (attempt/'data.json').write_text('{}')
    path = attempt/module._REVIEW_SESSION_NAME
    parent, inherited = socket.socketpair()
    class OriginalBridge:
        def inherited_fd(self):return inherited.fileno()
        def launched(self):pass
    observed = []
    def cancelled():
        if not path.exists():
            return False
        row = json.loads(path.read_bytes()); url = row['receiver_url']
        with urllib.request.urlopen(url, timeout=2) as response:
            assert response.status == 200
        observed.append(url)
        if drift:
            path.write_text('{"foreign":true}')
        return True
    try:
        expected = HostError if drift else HostCancelled
        with pytest.raises(expected):
            host._run_command_definition(record, {'html':str(attempt/'inputs.html'),
                'data':str(attempt/'data.json'), 'no_open':True, 'timeout':10}, attempt/'outputs', attempt,
                cancelled=cancelled, admission=admission, child_bridge=OriginalBridge())
        assert len(observed)==1 and not host._active_processes
        with pytest.raises((urllib.error.URLError, ConnectionError, TimeoutError)):
            urllib.request.urlopen(observed[0], timeout=1)
        if drift:
            assert path.read_text() == '{"foreign":true}'
        else:
            assert not host._unresolved_task_scopes and not path.exists()
            if keep_attempt:
                assert attempt.is_dir()
            else:
                host._cleanup_ephemeral_attempt_or_latch(attempt)
                assert not attempt.exists()
        assert host.client.heartbeat_progress == []
    finally:
        inherited.close(); parent.close()


@pytest.mark.parametrize('field,value', [('process_pid', -1), ('process_birth_id', 'foreign'),
                                        ('receiver_url', 'http://example.com:12345/?token='+'a'*32)])
def test_review_descriptor_cannot_adopt_foreign_process_or_receiver(tmp_path, field, value):
    from types import SimpleNamespace
    from astrid.core.execution import generic_host as module
    attempt = tmp_path.resolve()
    path = attempt/module._REVIEW_SESSION_NAME
    birth = module.process_birth_identity()
    row = {'schema':'astrid.human-review-session/v1','process_pid':os.getpid(),
           'process_birth_id':birth,'receiver_url':'http://127.0.0.1:12345/?token='+'a'*32}
    row[field]=value;path.write_text(json.dumps(row));path.chmod(0o600)
    original = SimpleNamespace(pid=os.getpid(), _astrid_process_birth=birth)
    with pytest.raises(HostError, match='original child'):
        module._observe_owned_review_session(original, attempt)
    with pytest.raises(HostError, match='mismatch'):
        module._retire_owned_review_session(attempt, None)
    assert path.exists()


@pytest.mark.parametrize('kind', ['fifo', 'symlink', 'public-mode'])
def test_review_descriptor_unsafe_file_is_bounded_and_retained(tmp_path, kind):
    from types import SimpleNamespace
    from astrid.core.execution import generic_host as module
    attempt = tmp_path.resolve(); path = attempt/module._REVIEW_SESSION_NAME
    if kind == 'fifo':
        os.mkfifo(path, 0o600)
    elif kind == 'symlink':
        target = attempt/'foreign'; target.write_text('{}'); path.symlink_to(target)
    else:
        path.write_text('{}'); path.chmod(0o644)
    owner = SimpleNamespace(pid=os.getpid(), _astrid_process_birth=module.process_birth_identity())
    with pytest.raises((HostError, OSError)):
        module._observe_owned_review_session(owner, attempt)
    with pytest.raises((HostError, OSError)):
        module._retire_owned_review_session(attempt, None)
    assert path.exists() or path.is_symlink()


@pytest.mark.parametrize("mismatch", [None, "actor_token", "target_token", "reference"])
def test_host_only_cleanup_consumes_real_custody_reference_verifier(tmp_path, monkeypatch, mismatch):
    from types import SimpleNamespace
    from astrid.core.execution import generic_host as module
    from astrid.core.execution import custody_broker as custody
    host_pid = os.getpid()
    owner_pid = host_pid + 1000000
    identities = {owner_pid: {"pid": owner_pid, "uid": os.getuid(), "birth_id": "runtime-owner-birth"},
                  host_pid: {"pid": host_pid, "uid": os.getuid(), "birth_id": module.process_birth_identity()}}
    tokens = {pid: {"pid": pid, "uid": os.getuid(), "pidversion": pid,
                    "sha256": f"sha256:fixture-{pid}", "words": [pid] * 8} for pid in identities}
    monkeypatch.setattr(custody, "audit_token_details", lambda words: {k: v for k, v in tokens[words[0]].items() if k != "words"})
    actor = custody.AuthenticatedCleanupActor(lambda: tokens[owner_pid], identities.get)
    scope = tmp_path / "real-authority"
    authority = custody.RoleCustodyAuthority(scope, "host")
    authority.designate_pending(actor=actor, identity=identities[host_pid], token=tokens[host_pid], owner_epoch="epoch")
    authority.bind_target(actor=actor, generation=1, identity=identities[host_pid], token=tokens[host_pid])
    reference = authority.reference()
    before = authority.path.read_bytes()
    preparation = module.LocalExecutionPreparation(operation_id="op", channel_id="ch")
    profile = {"execution_mode": "host_only_cpu", "profile_digest": "sha256:" + "1" * 64}
    preparation._request = {"owner_epoch": "epoch", "profile": profile, "custody_scope": str(scope)}
    preparation._retained_relay_actor = actor
    calls = []
    preparation._claim_host = SimpleNamespace(shutdown=lambda: calls.append("shutdown"))
    observed_tokens = {pid: dict(row) for pid, row in tokens.items()}
    if mismatch in {"actor_token", "target_token"}:
        observed_tokens[owner_pid if mismatch == "actor_token" else host_pid]["pidversion"] += 1
    elif mismatch == "reference": reference["generation"] += 1
    monkeypatch.setattr(custody, "default_process_identity", identities.get)
    monkeypatch.setattr(custody, "current_process_audit_token", observed_tokens.get)
    monkeypatch.setattr(custody, "_atomic_owner_json", lambda *_: pytest.fail("host reference verification wrote"))
    monkeypatch.setattr(custody, "signal_audit_token", lambda *_: pytest.fail("host reference verification signalled"))
    request = {"version": module._LOCAL_PREPARATION_VERSION, "command": "abort_local_execution",
               "operation_id": "op", "channel_id": "ch", "owner_epoch": "epoch",
               "profile_digest": profile["profile_digest"], "profile_binding_digest": module._handoff_digest(profile),
               "custody_scope": str(scope), "custody_capabilities": {"host": reference}}
    if mismatch is None:
        reply = preparation.dispatch(request)
        assert reply["status"] == "cleaned" and reply["cleanup"] == {"host_children": "absent"}
        assert calls == ["shutdown"] and preparation._aborted
    else:
        with pytest.raises(custody.CustodyError): preparation.dispatch(request)
        assert calls == [] and not preparation._aborted and preparation._abort_ack is None
    assert authority.path.read_bytes() == before


@pytest.mark.parametrize("claim_shape", ["mapping", "typed"])
def test_blocked_claim_without_attempt_preserves_ready_host(tmp_path, monkeypatch, claim_shape):
    """A real host may observe occupied resources without owning an attempt."""
    from types import SimpleNamespace
    _, host, runtime, _, _ = pack(tmp_path)
    offered = {"waiting_reason": "waiting_for_cpu",
               "task": {"task_id": "queued-child", "capability_id": "unrelated.calculate"}}
    if claim_shape == "typed":
        offered = SimpleNamespace(**offered)
    observed = []
    def claim_next(**kwargs):
        observed.append(kwargs)
        return offered
    monkeypatch.setattr(runtime, "claim_next", claim_next)
    monkeypatch.setattr(host, "run_task", lambda *a, **kw: pytest.fail("blocked claim must not execute or acquire attempt custody"))
    assert host.claim_once() is None
    assert host.claim_once() is None
    assert len(observed) == 2
    assert all(row["executor_id"] == host.executor_id for row in observed)
    assert host.handoff_quiescence()["claim_rpc_in_flight"] == 0
    assert host.handoff_quiescence()["active_attempts"] == 0
    assert not host._unresolved_task_scopes
    assert runtime.failures == [] and runtime.settlements == []


@pytest.mark.parametrize('case', [
    'owned', 'existing_attempts', 'missing_output', 'wrong_output', 'missing_support',
    'wrong_support', 'support_alias', 'output_alias', 'attempts_alias',
    'attempt_root', 'attempt_base', 'standalone', 'bootstrap', 'aborted',
])
def test_cli_retained_host_only_owns_canonical_attempt_namespace(tmp_path, monkeypatch, case):
    """Real prepared owner reaches CLI; external activation/auth I/O stays at its boundary."""
    from types import SimpleNamespace
    from astrid.core.execution import generic_host as module
    from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash
    support = tmp_path / 'support'; support.mkdir()
    output = support / 'host-output'; output.mkdir()
    boot = support / 'astrid-host/boot-manifest.json'; boot.parent.mkdir()
    boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
    executable = Path(sys.executable)
    launch = {'source_checkout': str(tmp_path), 'pack_root': str(tmp_path / 'packs'),
              'boot_manifest_path': str(boot),
              'boot_manifest_hash': 'sha256:' + load_boot_manifest_hash(boot, support_root=support),
              'capability_matrix': None, 'environment': {}, 'max_concurrency': 4}
    profile = {'execution_mode': 'host_only_cpu', 'profile_id': 'fixture', 'workspace_uuid': 'realm',
               'realm_root': str(tmp_path / 'realm'), 'support_root': str(support), 'machine_id': 'machine',
               'host_executable': str(executable),
               'host_artifact_digest': 'sha256:' + module.hashlib.sha256(executable.read_bytes()).hexdigest(),
               'session_config_digest': module._handoff_digest(launch), 'profile_revision': 'revision',
               'profile_digest': 'sha256:' + '1'*64, 'release_digest': 'sha256:' + '2'*64,
               'host_protocol': {'version': module.CURRENT_ROLES_VERSION,
                   'module': 'astrid.core.execution.generic_host', 'source_closure_digest': '3'*64},
               'host_launch': launch}
    owner = {'pid': 123, 'uid': os.getuid(), 'birth_id': 'owner',
             'runtime_instance_id': 'epoch', 'coordinator_epoch': 'epoch'}
    prepared = module.LocalExecutionPreparation(operation_id='op', channel_id='ch',
        graph_factory=lambda **kw: pytest.fail('host-only allocated an engine'))
    prepared._retained_relay_actor = SimpleNamespace(verify=lambda: owner)
    monkeypatch.setattr(module, 'source_checkout_closure_digest', lambda path: '3'*64)
    prepared.dispatch({'version': module._LOCAL_PREPARATION_VERSION, 'command': 'prepare_local_execution',
        'operation_id': 'op', 'channel_id': 'ch', 'owner_epoch': 'epoch', 'runtime_owner': owner,
        'profile': profile, 'custody_scope': str(tmp_path / 'scope')})
    grant = {'version': module._ACTIVATION_VERSION, 'operation_id': 'op', 'channel_id': 'ch',
             'executor_incarnation': 'incarnation', 'evidence_digest': 'sha256:' + '4'*64}
    credential = tmp_path / 'credential'; credential.write_text('test-only'); credential.chmod(0o600)
    argv = ['host', '--pack-root', str(tmp_path / 'packs'), '--support-root', str(support)]
    if case != 'standalone':
        argv += ['--runtime-endpoint', 'http://127.0.0.1:1', '--credential-file', str(credential),
                 '--activation-fd', '4', '--activation-operation-id', 'op', '--activation-channel-id', 'ch']
        if case != 'bootstrap': argv += ['--host-control-fd', '5']
    if case == 'bootstrap': grant['acceptance_mode'] = module._ACTIVATION_BOOTSTRAP_MODE
    if case == 'aborted': prepared._aborted = True
    if case == 'missing_support': del argv[argv.index('--support-root'):argv.index('--support-root')+2]
    if case == 'wrong_support':
        other = tmp_path / 'other'; other.mkdir(); argv[argv.index('--support-root')+1] = str(other)
    if case == 'support_alias':
        alias = tmp_path / 'alias'; alias.symlink_to(support, target_is_directory=True)
        argv[argv.index('--support-root')+1] = str(alias)
    if case == 'output_alias':
        saved = support / 'saved-output'; output.rename(saved); output.symlink_to(saved, target_is_directory=True)
    if case == 'attempts_alias':
        (support / 'elsewhere').mkdir(); (output / 'attempts').symlink_to(support / 'elsewhere', target_is_directory=True)
    if case == 'existing_attempts': (output / 'attempts').mkdir()
    if case == 'attempt_root': argv += ['--attempt-root', str(tmp_path / 'override')]
    if case == 'attempt_base': argv += ['--attempt-base', str(tmp_path / 'override')]
    monkeypatch.setenv('ASTRID_OUTPUT_ROOT', str(tmp_path if case == 'wrong_output' else output))
    if case == 'missing_output': monkeypatch.delenv('ASTRID_OUTPUT_ROOT')
    monkeypatch.delenv('ASTRID_REMOTION_VALIDATION_LAUNCH', raising=False)
    monkeypatch.delenv('ASTRID_EXECUTION_TARGET_JSON', raising=False)
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(module.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(prepared, 'serve_control', lambda fd: None)
    monkeypatch.setattr(module, 'LocalExecutionPreparation', lambda **kw: prepared)
    monkeypatch.setattr(module, '_await_worker_activation', lambda *a, **kw: grant)
    monkeypatch.setattr(module, '_await_enabled_runtime_credential', lambda *a, **kw: None)
    monkeypatch.setattr(module, 'RuntimeProtocolClient', lambda *a, credential_file=None: object())
    monkeypatch.setattr(module, '_compose_cli_boot_manifest', lambda *a: (boot, launch['boot_manifest_hash']))
    monkeypatch.setattr(module, '_startup_identity_attestation', lambda **kw: {'source': {'closure_digest': '3'*64}})
    captured = {}
    class ReachedHost(Exception): pass
    real_host = module.GenericPackHost
    def construct(**kwargs):
        captured.update(kwargs)
        raise ReachedHost
    monkeypatch.setattr(module, 'GenericPackHost', construct)
    try:
        if case not in ('owned', 'existing_attempts', 'standalone', 'bootstrap'):
            with pytest.raises(SystemExit) as error: module._cli()
            assert error.value.code == 2 and not captured
            return
        with pytest.raises(ReachedHost): module._cli()
        if case == 'standalone':
            assert captured['attempt_base'] is None
            return
        assert captured['attempt_base'] == str(output / 'attempts')
        host = real_host(pack_roots=[], attempt_base=captured['attempt_base'])
        attempt = host._allocate_attempt_root('task', 'attempt')
        assert attempt == support / 'host-output/attempts/task-attempt'
        # The actual managed receiver derives its descriptor from this same root.
        from astrid.packs.editorial.actions.human_review import run as review
        from astrid.sdk._child_bridge import ChildBridge
        parent, channel = socket.socketpair()
        bridge = ChildBridge(channel)
        monkeypatch.setattr(review, '_inherited_bridge', lambda: bridge)
        (attempt / 'outputs').mkdir()
        monkeypatch.setenv('ASTRID_PROGRESS_PATH', str(attempt / '.astrid-progress.json'))
        try:
            assert review._managed_receiver_path(attempt / 'outputs/review.html') == attempt / '.astrid-human-review-session.json'
        finally:
            bridge.close(); parent.close()
    finally:
        if prepared._handoff_lock_fd is not None: os.close(prepared._handoff_lock_fd)


@pytest.mark.parametrize("shared_accounting", [False, True])
@pytest.mark.parametrize("keep_attempt", [False, True])
def test_cpu_shutdown_waits_for_real_original_attempt_release(tmp_path, monkeypatch, shared_accounting, keep_attempt):
    import threading
    import time
    from astrid.core.execution.generic_host import _ManagedTaskAdapter
    from astrid.core.execution.process_group import verify_tree_absent

    entered = tmp_path / "shutdown-entered"
    _, coordinator, runtime, _, _ = pack(tmp_path, code=(
        "from pathlib import Path\nimport time\n"
        f"def run(value):\n    Path({str(entered)!r}).write_text('entered')\n"
        "    time.sleep(30)\n    return value\n"))
    coordinator.register()
    if shared_accounting:
        monkeypatch.setattr(runtime, "fork_for_host_slot", lambda: runtime, raising=False)
        host = coordinator._new_claim_slot()
    else:
        host = coordinator
    owned = []
    track = host._track_process
    def retain(process):
        owned.append(process)
        track(process)
    monkeypatch.setattr(host, "_track_process", retain)
    results, failures = [], []
    def serve():
        try:
            results.append(host.run_task(task_for(runtime), lease_token="lease", keep_attempt=keep_attempt))
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=serve)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not entered.exists() and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert entered.exists()
        assert isinstance(host.managed_tool_session.current_adapter, _ManagedTaskAdapter)
        assert coordinator.handoff_quiescence()["active_attempts"] == 1
        host.shutdown()
    finally:
        host._shutdown.set()
        worker.join(timeout=10)
    assert not worker.is_alive() and not failures
    assert len(results) == 1 and results[0]["cancelled"] is True
    assert coordinator.handoff_quiescence()["active_attempts"] == 0
    assert host.managed_tool_session.current_adapter is None
    assert not host._unresolved_task_scopes and not host._active_processes
    assert len(owned) == 1 and owned[0].returncode is not None
    from astrid.core.execution import task_scope as _ts; assert _ts.stop(owned[0], 0.1).status == "settled"
    assert runtime.settlements == [] and runtime.failures == []


@pytest.mark.parametrize("uncertainty", ["observation"])
def test_cpu_shutdown_cannot_close_unproved_real_attempt(tmp_path, monkeypatch, uncertainty):
    import threading
    import time
    from astrid.core.execution.process_group import CleanupUncertainError, verify_tree_absent

    entered = tmp_path / "uncertain-shutdown-entered"
    _, host, runtime, _, _ = pack(tmp_path, code=(
        "from pathlib import Path\nimport time\n"
        f"def run(value):\n    Path({str(entered)!r}).write_text('entered')\n"
        "    time.sleep(30)\n    return value\n"))
    host.register()
    owned, failures = [], []
    track = host._track_process
    def retain(process):
        owned.append(process)
        track(process)
    monkeypatch.setattr(host, "_track_process", retain)
    def serve():
        try:
            host.run_task(task_for(runtime), lease_token="lease")
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=serve)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not entered.exists() and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert entered.exists()
        host._handoff_observation_known = False
        with pytest.raises(CleanupUncertainError, match="CPU attempt cleanup remains unresolved"):
            host.shutdown()
        assert host._unresolved_task_scopes
    finally:
        host._shutdown.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(owned) == 1 and owned[0].returncode is not None
    from astrid.core.execution import task_scope as _ts; assert _ts.stop(owned[0], 0.1).status == "settled"
    assert host._unresolved_task_scopes
    assert runtime.settlements == [] and runtime.failures == []


@pytest.mark.posix_native
def test_startup_phase_row_is_fixed_numeric_and_preserves_fd_flags(tmp_path, monkeypatch):
    import fcntl
    from astrid.core.execution import generic_host as module
    log = tmp_path / 'private-startup.log'
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    saved = os.dup(2)
    monkeypatch.setattr(module, '_STARTUP_OBSERVATION_ENABLED', True)
    monkeypatch.setenv('PRIVATE_TOKEN', 'never-emit-private-sentinel')
    try:
        os.dup2(descriptor, 2)
        before = fcntl.fcntl(2, fcntl.F_GETFL)
        module._emit_startup_phase('startup_identity_begin')
        after = fcntl.fcntl(2, fcntl.F_GETFL)
        # Darwin may add FWASWRITTEN, which is kernel bookkeeping rather than
        # a caller flag. The emitter never calls F_SETFL.
        caller_flags = os.O_ACCMODE | os.O_APPEND | os.O_NONBLOCK | getattr(os, 'O_SYNC', 0)
        assert (before & caller_flags) == (after & caller_flags)
    finally:
        os.dup2(saved, 2); os.close(saved); os.close(descriptor)
    data = log.read_bytes()
    assert len(data) <= 512 and data.endswith(b'\n')
    row = json.loads(data)
    assert set(row) == {'diagnostic', 'phase', 'process_id', 'elapsed_ms'}
    assert row['diagnostic'] == 'astrid.host_startup_phase'
    assert row['phase'] == 'startup_identity_begin'
    assert row['process_id'] == os.getpid() and type(row['elapsed_ms']) is int and row['elapsed_ms'] >= 0
    assert b'never-emit-private-sentinel' not in data and str(log).encode() not in data


@pytest.mark.parametrize('case', ['disabled', 'phase', 'mode', 'owner', 'full_pipe', 'socket'])
@pytest.mark.posix_native
def test_startup_phase_skips_unadmitted_sink_without_flag_changes(tmp_path, monkeypatch, case):
    import fcntl
    from types import SimpleNamespace
    from astrid.core.execution import generic_host as module
    saved = os.dup(2); descriptors = []; connections = []
    log = tmp_path / 'private-startup.log'
    monkeypatch.setattr(module, '_STARTUP_OBSERVATION_ENABLED', case != 'disabled')
    try:
        if case == 'full_pipe':
            read, write = os.pipe(); descriptors.extend([read, write])
            flags = fcntl.fcntl(write, fcntl.F_GETFL)
            fcntl.fcntl(write, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            while True:
                try: os.write(write, b'x' * 4096)
                except BlockingIOError: break
            fcntl.fcntl(write, fcntl.F_SETFL, flags)
            os.dup2(write, 2)
        elif case == 'socket':
            left, right = socket.socketpair(); connections.extend([left, right]); os.dup2(left.fileno(), 2)
        else:
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            descriptors.append(descriptor); os.dup2(descriptor, 2)
            if case == 'mode': os.fchmod(descriptor, 0o644)
            if case == 'owner':
                actual_fstat = os.fstat
                def foreign_owner(fd):
                    info = actual_fstat(fd)
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1) if fd == 2 else info
                monkeypatch.setattr(module.os, 'fstat', foreign_owner)
        before = fcntl.fcntl(2, fcntl.F_GETFL)
        module._emit_startup_phase('never-emit-secret-input' if case == 'phase' else 'preflight_begin')
        assert fcntl.fcntl(2, fcntl.F_GETFL) == before
    finally:
        os.dup2(saved, 2); os.close(saved)
        for descriptor in descriptors: os.close(descriptor)
        for connection in connections: connection.close()
    if log.exists(): assert log.read_bytes() == b''


def test_startup_phase_write_failure_cannot_replace_original_exception(tmp_path, monkeypatch):
    from astrid.core.execution import generic_host as module
    saved = os.dup(2); descriptor = os.open(tmp_path / 'private.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    monkeypatch.setattr(module, '_STARTUP_OBSERVATION_ENABLED', True)
    original = HostError('private-original-sentinel')
    actual_write = os.write
    def failed_write(fd, data):
        if fd == 2: raise OSError(5, 'private-write-sentinel')
        return actual_write(fd, data)
    try:
        os.dup2(descriptor, 2)
        monkeypatch.setattr(module.os, 'write', failed_write)
        with pytest.raises(HostError) as error:
            try: raise original
            finally: module._emit_startup_phase('preflight_begin')
        assert error.value is original
    finally:
        os.dup2(saved, 2); os.close(saved); os.close(descriptor)


@pytest.mark.parametrize('fault', [None, 'identity', 'constructor', 'discovery', 'preflight', 'registration'])
def test_cli_startup_phase_orders_real_consumer_boundaries(tmp_path, monkeypatch, fault):
    from astrid.core.execution import generic_host as module
    from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash
    boot = tmp_path / 'boot-manifest.json'; boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
    digest = 'sha256:' + load_boot_manifest_hash(boot, support_root=tmp_path)
    ready = tmp_path / 'ready.json'
    argv = ['host', '--pack-root', str(tmp_path / 'packs'), '--ready-file', str(ready)]
    if fault == 'registration': argv.append('--register')
    for name in ('ASTRID_REMOTION_VALIDATION_LAUNCH', 'ASTRID_REMOTION_VALIDATION_ROLE', 'ASTRID_EXECUTION_TARGET_JSON', 'ASTRID_OUTPUT_ROOT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(module.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(module.faulthandler, 'register', lambda *a, **k: None)
    monkeypatch.setattr(module, '_compose_cli_boot_manifest', lambda *a: (boot, digest))
    rows = []; monkeypatch.setattr(module, '_emit_startup_phase', rows.append)
    original = HostError('never-emit-original-sentinel')
    def identity(**kwargs):
        if fault == 'identity': raise original
        return {'source': {'closure_digest': '3' * 64}}
    monkeypatch.setattr(module, '_startup_identity_attestation', identity)
    actual_constructor = module.GenericPackHost
    def construct(**kwargs):
        if fault == 'constructor': raise original
        host = actual_constructor(**kwargs)
        def discover():
            if fault == 'discovery': raise original
            return []
        def preflight():
            if fault == 'preflight': raise original
            return None
        monkeypatch.setattr(host, 'discover', discover); monkeypatch.setattr(host, 'preflight', preflight)
        if fault == 'registration':
            def register(): raise original
            monkeypatch.setattr(host, 'register', register)
        return host
    monkeypatch.setattr(module, 'GenericPackHost', construct)
    expected = ['cli_args_parsed', 'startup_identity_begin']
    if fault != 'identity': expected += ['startup_identity_done', 'host_constructor_begin']
    if fault not in ('identity', 'constructor'): expected += ['host_constructor_done', 'discovery_begin']
    if fault not in ('identity', 'constructor', 'discovery'): expected += ['discovery_done']
    if fault not in ('identity', 'constructor', 'discovery', 'registration'): expected += ['preflight_begin']
    if fault is None: expected += ['preflight_done', 'registration_begin']
    if fault == 'registration': expected += ['registration_begin']
    if fault is None: expected += ['registration_done', 'ready_written']
    if fault == 'identity':
        with pytest.raises(SystemExit): module._cli()  # Existing parser-error contract unchanged.
    elif fault == 'registration':
        assert module._cli() == 1
        assert json.loads(ready.read_text())['status'] == 'failed'
    elif fault:
        with pytest.raises(HostError) as error: module._cli()
        assert error.value is original
    else:
        assert module._cli() == 0
        assert json.loads(ready.read_text())['status'] == 'ready'
    assert rows == expected


@pytest.mark.parametrize('mode', ['register', 'listing', 'task', 'preflight_failure', 'registration_failure'])
def test_cli_registration_runs_one_real_preflight_inside_fence(tmp_path, monkeypatch, mode):
    from astrid.core.execution import generic_host as module
    from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash

    root, host, runtime, _, _ = pack(tmp_path)
    boot = tmp_path / 'boot-manifest.json'
    boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
    digest = 'sha256:' + load_boot_manifest_hash(boot, support_root=tmp_path)
    ready = tmp_path / 'ready.json'
    credential = tmp_path / 'worker.token'; credential.write_text('unit-worker'); credential.chmod(0o600)
    argv = ['host', '--pack-root', str(root), '--ready-file', str(ready),
            '--runtime-endpoint', 'http://127.0.0.1:1', '--credential-file', str(credential)]
    registered = mode in ('register', 'preflight_failure', 'registration_failure')
    if registered:
        argv.append('--register')
    if mode == 'task':
        argv += ['--run-task', 'unit-task', '--lease-token', 'unit-lease']
        monkeypatch.setattr(runtime, 'task', lambda task_id: {'id': task_id})
        monkeypatch.setattr(host, 'run_task', lambda task, **kw: {'task': task})
    for name in ('ASTRID_REMOTION_VALIDATION_LAUNCH', 'ASTRID_REMOTION_VALIDATION_ROLE',
                 'ASTRID_EXECUTION_TARGET_JSON', 'ASTRID_OUTPUT_ROOT', 'ASTRID_HOST_READINESS_PROFILE_PATH'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(module.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(module.faulthandler, 'register', lambda *a, **k: None)
    monkeypatch.setattr(module, '_compose_cli_boot_manifest', lambda *a: (boot, digest))
    monkeypatch.setattr(module, '_startup_identity_attestation', lambda **kw: {'source': {'closure_digest': '3' * 64}})
    monkeypatch.setattr(module, 'RuntimeProtocolClient', lambda *a, credential_file=None: runtime)
    monkeypatch.setattr(module, 'GenericPackHost', lambda **kw: host)
    phases = []; monkeypatch.setattr(module, '_emit_startup_phase', phases.append)
    checked = []; original_preflight = host.preflight
    failure = HostError('unit original failure')

    def preflight(*args, **kwargs):
        checked.append(host.handoff_quiescence()['registration_rpc_in_flight'])
        if mode == 'preflight_failure':
            raise failure
        return original_preflight(*args, **kwargs)

    monkeypatch.setattr(host, 'preflight', preflight)
    if mode == 'registration_failure':
        class RuntimeRejection(Exception):
            code = 'registration_unavailable'
            request_id = 'unit-request-1'
            status = 503
            message = 'executor registration unavailable for unit-worker'
        def fail_registration(*args, **kwargs):
            raise RuntimeRejection()
        monkeypatch.setattr(runtime, 'register_executor', fail_registration)
    result = module._cli()  # Real register/_register_impl and real CPU preflight.
    assert checked == [1 if registered else 0]
    assert host.handoff_quiescence()['registration_rpc_in_flight'] == 0
    assert phases.count('preflight_begin') == 1
    marker = json.loads(ready.read_text())
    if mode in ('preflight_failure', 'registration_failure'):
        assert result == 1 and marker['status'] == 'failed'
        assert 'ready_written' not in phases and runtime.registrations == []
        if mode == 'registration_failure':
            assert marker['error'] == {'code': 'registration_unavailable',
                'request_id': 'unit-request-1',
                'message': 'executor registration unavailable for [redacted]'}
    else:
        assert result == 0 and marker['status'] == 'ready'
        assert phases.count('preflight_done') == 1
        assert len(runtime.registrations) == int(registered)
    if registered:
        assert phases.index('registration_begin') < phases.index('preflight_begin')
    else:
        assert phases.index('preflight_begin') < phases.index('ready_written')


def test_managed_service_startup_failure_waits_for_requested_cleanup(tmp_path, monkeypatch):
    import io
    import threading

    from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash
    from astrid.core.execution import generic_host as module

    root, host, runtime, _, _ = pack(tmp_path)
    boot = tmp_path / 'boot-manifest.json'
    boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
    digest = 'sha256:' + load_boot_manifest_hash(boot, support_root=tmp_path)
    credential = tmp_path / 'worker.token'
    credential.write_text('unit-worker')
    credential.chmod(0o600)
    monkeypatch.setenv('ASTRID_PACK_HOST_SERVICE_MODE', '1')
    monkeypatch.setattr(sys, 'argv', [
        'host', 'run', '--pack-root', str(root), '--runtime-endpoint', 'http://127.0.0.1:1',
        '--credential-file', str(credential), '--register',
    ])
    for name in ('ASTRID_REMOTION_VALIDATION_LAUNCH', 'ASTRID_REMOTION_VALIDATION_ROLE',
                 'ASTRID_EXECUTION_TARGET_JSON', 'ASTRID_OUTPUT_ROOT', 'ASTRID_HOST_READINESS_PROFILE_PATH'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module.platform, 'install_stop_handlers', lambda *args: None)
    monkeypatch.setattr(module.faulthandler, 'register', lambda *args, **kwargs: None)
    monkeypatch.setattr(module, '_compose_cli_boot_manifest', lambda *args: (boot, digest))
    monkeypatch.setattr(module, '_startup_identity_attestation', lambda **kw: {'source': {'closure_digest': '3' * 64}})
    monkeypatch.setattr(module, 'RuntimeProtocolClient', lambda *args, credential_file=None: runtime)
    monkeypatch.setattr(module, 'GenericPackHost', lambda **kw: host)
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    completed = threading.Event()
    outcome = {}

    def shutdown():
        cleanup_entered.set()
        assert release_cleanup.wait(timeout=3)

    def discover():
        assert cleanup_entered.wait(timeout=2)
        raise module.HostError('discovery failed')

    monkeypatch.setattr(host, 'shutdown', shutdown)
    monkeypatch.setattr(host, 'discover', discover)
    lifecycle = module._ManagedStdinShutdown(host, io.StringIO(''))
    monkeypatch.setattr(module, '_ManagedStdinShutdown', lambda *args: lifecycle)

    def run_cli():
        try:
            outcome['result'] = module._cli()
        except BaseException as exc:  # noqa: BLE001 - assert the CLI's failure after cleanup completes.
            outcome['error'] = exc
        finally:
            completed.set()

    runner = threading.Thread(target=run_cli)
    try:
        runner.start()
        assert cleanup_entered.wait(timeout=2)
        assert not completed.wait(0.05)
        release_cleanup.set()
        assert completed.wait(timeout=2)
        assert lifecycle.cleanup_done.is_set()
        assert isinstance(outcome.get('error'), module.HostError)
        assert str(outcome['error']) == 'discovery failed'
    finally:
        release_cleanup.set()
        runner.join(timeout=3)
        assert not runner.is_alive()
        assert lifecycle.cleanup_done.wait(timeout=3)


def test_managed_service_registration_failure_does_not_publish_readiness(tmp_path, monkeypatch):
    from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash
    from astrid.core.execution import generic_host as module

    root, host, runtime, _, _ = pack(tmp_path)
    boot = tmp_path / 'boot-manifest.json'
    boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
    digest = 'sha256:' + load_boot_manifest_hash(boot, support_root=tmp_path)
    ready = tmp_path / 'ready.json'
    ready.write_text('{"status":"stale"}')
    credential = tmp_path / 'worker.token'
    credential.write_text('unit-worker')
    credential.chmod(0o600)
    monkeypatch.setenv('ASTRID_PACK_HOST_SERVICE_MODE', '1')
    monkeypatch.setattr(
        sys,
        'argv',
        ['host', '--pack-root', str(root), '--ready-file', str(ready),
         '--runtime-endpoint', 'http://127.0.0.1:1', '--credential-file', str(credential), '--register'],
    )
    for name in ('ASTRID_REMOTION_VALIDATION_LAUNCH', 'ASTRID_REMOTION_VALIDATION_ROLE',
                 'ASTRID_EXECUTION_TARGET_JSON', 'ASTRID_OUTPUT_ROOT', 'ASTRID_HOST_READINESS_PROFILE_PATH'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(module.faulthandler, 'register', lambda *a, **k: None)
    monkeypatch.setattr(module, '_compose_cli_boot_manifest', lambda *a: (boot, digest))
    monkeypatch.setattr(module, '_startup_identity_attestation', lambda **kw: {'source': {'closure_digest': '3' * 64}})
    monkeypatch.setattr(module, 'RuntimeProtocolClient', lambda *a, credential_file=None: runtime)
    monkeypatch.setattr(module, 'GenericPackHost', lambda **kw: host)
    class RuntimeRejection(Exception):
        code = 'registration_unavailable'
        request_id = 'unit-request-1'
        message = 'executor registration unavailable'
    monkeypatch.setattr(runtime, 'register_executor', lambda *a, **k: (_ for _ in ()).throw(RuntimeRejection()))

    assert module._cli() == 1
    assert not ready.exists()


@pytest.mark.parametrize('fail_cleanup', [False, True])
def test_managed_service_waits_for_eof_cleanup_and_propagates_failure(fail_cleanup):
    import os
    import threading
    from types import SimpleNamespace

    from astrid.core.execution import generic_host as module

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, 'r', encoding='utf-8')
    stopped = threading.Event()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    completed = threading.Event()
    outcome = {}

    def shutdown():
        stopped.set()
        cleanup_entered.set()
        assert release_cleanup.wait(timeout=2)
        if fail_cleanup:
            raise RuntimeError('cleanup failed')

    host = SimpleNamespace(_shutdown=stopped, shutdown=shutdown)
    lifecycle = module._ManagedStdinShutdown(host, stream)
    try:
        lifecycle.start()

        def run_managed_loop():
            try:
                outcome['result'] = lifecycle.run_loop(lambda: stopped.wait(timeout=2))
            except BaseException as exc:  # noqa: BLE001 - capture and assert thread cleanup failures.
                outcome['error'] = exc
            finally:
                completed.set()

        runner = threading.Thread(target=run_managed_loop)
        runner.start()
        os.close(write_fd)
        write_fd = -1
        assert cleanup_entered.wait(timeout=2)
        assert not completed.wait(0.05)
        release_cleanup.set()
        assert completed.wait(timeout=2)
        runner.join(timeout=2)
        assert not runner.is_alive()
        if fail_cleanup:
            assert isinstance(outcome.get('error'), RuntimeError)
            assert str(outcome['error']) == 'cleanup failed'
        else:
            assert outcome == {'result': True}
    finally:
        release_cleanup.set()
        stream.close()
        if write_fd >= 0:
            os.close(write_fd)


def test_managed_service_does_not_wait_for_eof_after_signal_shutdown():
    import os
    import signal
    import threading
    from types import SimpleNamespace

    from astrid.core.execution import generic_host as module

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, 'r', encoding='utf-8')
    stopped = threading.Event()
    host = SimpleNamespace(_shutdown=stopped, shutdown=stopped.set)
    lifecycle = module._ManagedStdinShutdown(host, stream)
    try:
        lifecycle.start()
        lifecycle.request_shutdown(signal.SIGTERM)
        assert lifecycle.run_loop(lambda: stopped.wait(timeout=2) and 'stopped') == 'stopped'
    finally:
        os.close(write_fd)
        stream.close()


@pytest.mark.parametrize('fail_cleanup', [False, True])
def test_managed_service_coalesces_signal_and_eof_shutdown(fail_cleanup):
    import os
    import signal
    import threading
    from types import SimpleNamespace

    from astrid.core.execution import generic_host as module

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, 'r', encoding='utf-8')
    stopped = threading.Event()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    completed = threading.Event()
    calls = []
    outcome = {}

    def shutdown():
        calls.append('cleanup')
        stopped.set()
        cleanup_entered.set()
        assert release_cleanup.wait(timeout=2)
        if fail_cleanup:
            raise RuntimeError('cleanup failed')

    host = SimpleNamespace(_shutdown=stopped, shutdown=shutdown)
    lifecycle = module._ManagedStdinShutdown(host, stream)
    try:
        lifecycle.start()
        lifecycle.request_shutdown(signal.SIGTERM)
        os.close(write_fd)
        write_fd = -1

        def run_managed_loop():
            try:
                outcome['result'] = lifecycle.run_loop(lambda: stopped.wait(timeout=2))
            except BaseException as exc:  # noqa: BLE001 - capture cleanup owner failure.
                outcome['error'] = exc
            finally:
                completed.set()

        runner = threading.Thread(target=run_managed_loop)
        runner.start()
        assert cleanup_entered.wait(timeout=2)
        assert calls == ['cleanup']
        assert not completed.wait(0.05)
        release_cleanup.set()
        assert completed.wait(timeout=2)
        runner.join(timeout=2)
        assert not runner.is_alive()
        assert calls == ['cleanup']
        if fail_cleanup:
            assert isinstance(outcome.get('error'), RuntimeError)
            assert str(outcome['error']) == 'cleanup failed'
        else:
            assert outcome == {'result': True}
    finally:
        release_cleanup.set()
        stream.close()
        if write_fd >= 0:
            os.close(write_fd)


def test_each_standalone_registration_rechecks_inside_original_fence(tmp_path, monkeypatch):
    _, host, runtime, _, _ = pack(tmp_path)
    original = host.preflight; checked = []
    def preflight(*args, **kwargs):
        checked.append(host.handoff_quiescence()['registration_rpc_in_flight'])
        return original(*args, **kwargs)
    monkeypatch.setattr(host, 'preflight', preflight)
    host.register()
    host.register(deliberate=True)
    assert checked == [1, 1] and len(runtime.registrations) == 2
    assert host.handoff_quiescence()['registration_rpc_in_flight'] == 0


@pytest.mark.parametrize('change', ['source', 'epoch', 'removed'])
def test_current_contract_registration_invalidation_requires_deliberate_refresh(tmp_path, monkeypatch, change):
    root, host, runtime, _, _ = pack(tmp_path)
    manifest = yaml.safe_load((root / 'pack.yaml').read_text())
    manifest['actions']['retained'] = dict(manifest['actions']['calculate'])
    (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    host.discover(); host.register()
    if change == 'source':
        (root / 'actions/hello.py').write_text("def run(value='default'):\n    return 'changed'\n")
        host.refresh()
        reason = 'source digest changed'
    elif change == 'epoch':
        original_health = runtime.health
        monkeypatch.setattr(runtime, 'health', lambda: {**original_health(), 'runtime_epoch': 2})
        reason = 'runtime epoch changed'
    else:
        manifest['actions'].pop('calculate')
        (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
        host.refresh()
        reason = 'capability removed'
    with pytest.raises(HostError, match=reason):
        host.register()
    assert len(runtime.registrations) == 1
    assert host.handoff_quiescence()['registration_rpc_in_flight'] == 0
    host.register(deliberate=True)
    assert len(runtime.registrations) == 2
    if change == 'removed':
        assert runtime.capability_registrations[-1][0] == 'unrelated.calculate'
        assert runtime.capability_registrations[-1][1]['status'] == 'unavailable'


@pytest.mark.parametrize('replacement', [False, True])
def test_current_contract_failed_registration_preserves_typed_error_and_atomic_publication(tmp_path, monkeypatch, replacement):
    from astrid.core.execution.generic_host import HostRegistrationError
    root, host, runtime, _, _ = pack(tmp_path)
    if replacement:
        manifest = yaml.safe_load((root / 'pack.yaml').read_text())
        manifest['actions']['retained'] = dict(manifest['actions']['calculate'])
        (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
        host.discover(); host.register()
        manifest['actions'].pop('calculate')
        (root / 'pack.yaml').write_text(yaml.safe_dump(manifest)); host.refresh()
    before = dict(host._registered_state)
    class RuntimeRejection(Exception):
        code = 'registration_unavailable'
        request_id = 'unit-request-2'
        status = 503
        message = 'registration unavailable'
    def rejected(*args, **kwargs): raise RuntimeRejection()
    monkeypatch.setattr(runtime, 'register_executor', rejected)
    with pytest.raises(HostRegistrationError) as error:
        host.register(deliberate=replacement)
    assert error.value.code == 'registration_unavailable'
    assert error.value.request_id == 'unit-request-2' and error.value.status == 503
    assert isinstance(error.value.__cause__, RuntimeRejection)
    assert len(runtime.registrations) == int(replacement)
    assert runtime.capability_registrations == [] and host._registered_state == before
    assert host.handoff_quiescence()['registration_rpc_in_flight'] == 0


def _local_parent_declaration():
    return {"name": "Parent", "description": "Explicit local parent with managed children.", "invocation": {"kind": "command", "command": {
        "argv": ["{python_exec}", "-c", "pass"],
        "input_args": [{"input": "value", "flag": "--value"}]}},
        "inputs": [{"name": "value", "type": "string", "required": True}], "outputs": [],
        "metadata": {"local_orchestrator": True, "auto_forward_inputs": False},
        "graph": {"child_actions": ["fixture.child"], "child_orchestrators": []}}


@pytest.mark.parametrize("marker", ["true", 1, None, {}, []])
def test_local_parent_marker_rejects_non_boolean_before_projection(tmp_path, marker):
    from types import SimpleNamespace
    from astrid.core.execution.executor.actions import action_executor_definition, action_orchestrator_definition
    from astrid.core.execution.executor.schema import ExecutorValidationError
    action = _local_parent_declaration()
    action["metadata"]["local_orchestrator"] = marker
    source = SimpleNamespace(id="fixture", version="1.0", root=tmp_path)
    for projector in (action_executor_definition, action_orchestrator_definition):
        with pytest.raises(ExecutorValidationError, match="boolean"):
            projector(source, "parent", action)


def test_local_parent_projection_preserves_binding_graph_and_executor_parity(tmp_path):
    from types import SimpleNamespace
    from astrid.core.execution.executor.actions import action_executor_definition, action_orchestrator_definition
    from astrid.core.execution.executor.schema import ExecutorValidationError
    action = _local_parent_declaration()
    source = SimpleNamespace(id="fixture", version="1.0", root=tmp_path)
    parent = action_orchestrator_definition(source, "parent", action)
    assert parent.child_executors == ("fixture.child",)
    assert parent.runtime.command.input_args[0].input == "value"
    with pytest.raises(ExecutorValidationError, match="not a host executor"):
        action_executor_definition(source, "parent", action)
    action["metadata"]["local_orchestrator"] = False
    unmarked = action_executor_definition(source, "parent", action)
    del action["metadata"]["local_orchestrator"]
    ordinary = action_executor_definition(source, "parent", action)
    assert unmarked.command == ordinary.command == parent.runtime.command
    assert unmarked.inputs == ordinary.inputs == parent.inputs
    action["metadata"]["local_orchestrator"] = True
    action["invocation"] = {"kind": "python", "path": "actions/child.py", "function": "run"}
    with pytest.raises(ExecutorValidationError, match="command invocation"):
        action_orchestrator_definition(source, "parent", action)


def test_local_parent_host_filters_only_marked_action(tmp_path):
    root, host, runtime, definition, admission = pack(tmp_path)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    parent = _local_parent_declaration()
    parent["graph"]["child_actions"] = ["unrelated.calculate"]
    manifest["actions"]["parent"] = parent
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    current = GenericPackHost(pack_roots=[root], client=runtime)
    assert [record.id for record in current.discover()] == ["unrelated.calculate"]
    from astrid.core.execution.orchestrator.registry import load_default_registry
    registry = load_default_registry(project_root=tmp_path, extra_pack_roots=(str(root),))
    assert registry.get("unrelated.parent").child_executors == ("unrelated.calculate",)


def test_local_parent_collision_rejected_across_public_registries(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from astrid.core.execution.executor.actions import action_orchestrator_definition
    from astrid.core.execution.executor.registry import ExecutorRegistry, ExecutorRegistryError
    from astrid.core.execution.executor import registry as executors
    from astrid.core.execution.orchestrator import registry as orchestrators
    action = _local_parent_declaration()
    source = SimpleNamespace(id="fixture", version="1.0", root=tmp_path, actions={"parent": action})
    from astrid.core.execution.executor.actions import _action_definition
    child = _action_definition(source, "parent", action)
    monkeypatch.setattr(executors, "_discover_executor_packs", lambda **kwargs: (source,))
    monkeypatch.setattr(executors, "_load_pack_executors_from_packs", lambda packs: (child,))
    with pytest.raises(ExecutorRegistryError, match="collides"):
        executors.load_default_registry()
    parent = action_orchestrator_definition(source, "parent", action)
    monkeypatch.setattr(orchestrators, "_discover_orchestrator_packs", lambda **kwargs: ())
    monkeypatch.setattr(orchestrators, "_load_pack_orchestrators_from_packs", lambda packs: (parent,))
    with pytest.raises(orchestrators.OrchestratorRegistryError, match="collides"):
        orchestrators.load_default_registry(executor_registry=ExecutorRegistry((child,)))


@pytest.mark.parametrize("kind", ["native", "command"])
def test_v3_worker_probe_matches_actual_readiness_context(tmp_path, monkeypatch, kind):
    from unittest.mock import patch
    from astrid.core.execution import generic_host
    from astrid.core.rendering import remotion_runtime

    root, host, _, _, _ = pack(tmp_path)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    if kind == "command":
        manifest["actions"]["calculate"]["invocation"] = {
            "kind": "command", "command": {
                "argv": ["{python_exec}", "-c", "pass"],
                "input_args": [{"input": "value", "flag": "--value", "optional": True}],
                "env": {"LC_ALL": "C.UTF-8"},
            },
        }
        (root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    record, = host.discover()
    selected = tmp_path / "host" / "site-packages"
    ambient = tmp_path / "caller" / "site-packages"
    selected.mkdir(parents=True)
    ambient.mkdir(parents=True)
    launch = {
        "HOME": str(tmp_path), "PATH": "/usr/bin", "LANG": "C.UTF-8",
        "PYTHONPATH": str(selected), "ASTRID_REMOTION_PROJECT_DIR": str(tmp_path),
        "ASTRID_REMOTION_VALIDATION_ROLE": "host",
    }
    monkeypatch.setenv("PYTHONPATH", str(ambient))
    variants = generic_host._launch_worker_probe_environments((root,), launch)
    assert len(variants) == 3
    assert all(str(ambient) not in value for variant in variants for value in variant.values())
    captured = []

    def validated(project, *, environment):
        assert project == tmp_path
        assert environment["ASTRID_REMOTION_VALIDATION_ROLE"] == "worker"
        captured.append(remotion_runtime._registry_context(environment, generator=False))

    monkeypatch.setattr(remotion_runtime, "_validated_runtime", validated)
    # Only render-capable workers are validated at launch and checked at spawn.
    monkeypatch.setattr(generic_host, "_record_may_render", lambda _record: True)
    # Execution sees the explicit host launch, rather than the SDK caller's
    # ambient imports. Exercise the genuine final child environment builder.
    with patch.dict(os.environ, launch, clear=True):
        explicit = None
        if kind == "command":
            explicit = {
                "PYTHONPATH": os.pathsep.join((str(Path(generic_host.__file__).resolve().parents[3]),
                                              *generic_host._dependency_pythonpath())),
                **dict(record.definition.command.env),
            }
        host._child_environment(record, tmp_path / "attempt", explicit_env=explicit,
                                readiness_worker_kind=kind)
    index = 1 if kind == "native" else 2
    assert captured == [remotion_runtime._registry_context(variants[index], generator=False)]
    assert str(selected) in variants[index]["PYTHONPATH"]
    if kind == "command":
        assert variants[index]["PYTHONPATH"].split(os.pathsep)[:2] == [str(root), str(root.parent)]


@pytest.mark.parametrize("credential", ["HIVEMIND_CONTRIBUTOR_KEY", "HIVEMIND_ANON_KEY"])
def test_v3_startup_probes_omit_credentials_without_task_authority(tmp_path, credential):
    from astrid.core.execution.generic_host import _launch_worker_probe_environments
    root, _, _, _, _ = pack(tmp_path)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["actions"]["calculate"]["isolation"] = {
        "mode": "subprocess", "env_passthrough": ["PUBLIC_VALUE", credential]}
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    variants = _launch_worker_probe_environments((root,), {
        "PATH": "/usr/bin", "PUBLIC_VALUE": "fixed-public", credential: "never-export-sentinel",
        "UNDECLARED_TOKEN": "never-export-sentinel"})
    assert len(variants) == 3
    for variant in variants[1:]:
        assert variant["PUBLIC_VALUE"] == "fixed-public"
    for variant in variants:
        assert credential not in variant and "UNDECLARED_TOKEN" not in variant
        assert not any("never-export-sentinel" in value for value in variant.values())


def test_v3_startup_probes_reject_fixed_undeclared_secret(tmp_path):
    from astrid.core.execution.generic_host import _launch_worker_probe_environments
    root, _, _, _, _ = pack(tmp_path)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["actions"]["calculate"]["invocation"] = {
        "kind": "command", "command": {"argv": ["{python_exec}", "-c", "pass"],
        "input_args": [{"input": "value", "flag": "--value", "optional": True}],
        "env": {"UNDECLARED_TOKEN": "never-export-sentinel"}}}
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(HostError, match="undeclared secret environment variable"):
        _launch_worker_probe_environments((root,), {"PATH": "/usr/bin"})


def test_v3_worker_probes_match_exact_host_roots_and_exclusions(tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import _launch_worker_probe_environments
    root, _, runtime, _, _ = pack(tmp_path)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    parent = _local_parent_declaration()
    parent["graph"]["child_actions"] = ["unrelated.calculate"]
    parent["invocation"]["command"]["env"] = {"PATH": "/excluded-parent"}
    manifest["actions"]["parent"] = parent
    ordinary = json.loads(json.dumps(manifest["actions"]["calculate"]))
    ordinary["metadata"]["local_orchestrator"] = False
    manifest["actions"]["ordinary"] = ordinary
    malformed = json.loads(json.dumps(parent))
    malformed["metadata"]["local_orchestrator"] = "true"
    manifest["actions"]["malformed"] = malformed
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_manifest = json.loads(json.dumps(manifest))
    outside_manifest["id"] = "outside"
    outside_manifest["actions"] = {"parent": parent}
    outside_manifest["actions"]["parent"]["metadata"]["local_orchestrator"] = False
    outside_manifest["actions"]["parent"]["invocation"]["command"]["env"] = {"PATH": "/outside-root"}
    (outside / "pack.yaml").write_text(yaml.safe_dump(outside_manifest))
    host = GenericPackHost(pack_roots=[root], client=runtime)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(outside))
    assert {row.id for row in host.discover()} == {"unrelated.calculate", "unrelated.ordinary"}
    variants = _launch_worker_probe_environments((root,), {"PATH": "/usr/bin"})
    assert len(variants) == 5
    assert all(variant["PATH"] == "/usr/bin" for variant in variants)
    assert not any(str(outside) in variant.get("PYTHONPATH", "") for variant in variants)


@pytest.mark.parametrize("census", ["released", "live-group", "unavailable", "failure"])
def test_wan_release_darwin_uses_complete_native_census_without_ps(monkeypatch, census):
    from types import SimpleNamespace
    from astrid.core.execution import generic_host as gh
    adapter = gh._ManagedWanChildAdapter(spec={}, binding=object(), readiness_timeout=1,
                                        release_timeout=1, track=lambda p: None, untrack=lambda p: None)
    adapter.process = SimpleNamespace(pid=4242, poll=lambda: 0, returncode=0, stdin=None, stdout=None)
    monkeypatch.setattr(gh.sys, "platform", "darwin")
    monkeypatch.setattr(gh, "_release_owned_group", lambda process: None)
    def snapshot():
        if census == "failure": raise OSError("native census unavailable")
        if census == "unavailable": return {}
        return {7: SimpleNamespace(pgid=4242 if census == "live-group" else 7)}
    monkeypatch.setattr(gh, "_process_snapshot", snapshot)
    monkeypatch.setattr(gh, "subprocess", SimpleNamespace(run=lambda *a, **k: pytest.fail("Darwin release must not launch ps")))
    if census in {"unavailable", "failure"}:
        with pytest.raises((HostError, OSError)): adapter.release(reason="test")
    else:
        result = adapter.release(reason="test")
        assert result["released"] is (census == "released")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("phase", ["registry", "cleanup"])
def test_native_engine_graph_listener_commands_keep_platform_timeout_and_parsing(tmp_path, monkeypatch, platform, phase):
    import hashlib, json, subprocess
    from types import SimpleNamespace
    import astrid.core.execution.generic_host as gh
    import astrid.core.execution.custody_broker as cb
    monkeypatch.setattr(gh.sys, "platform", platform)
    root = tmp_path / "engine"; root.mkdir()
    birth = "ps-lstart:Mon Oct 12 20:00:00 2026"
    config = b"{}"; digest = "sha256:" + hashlib.sha256(config).hexdigest()
    for name, value in [("pid", "910001"), ("comfy_pid", "910002"), ("comfy_process_start_identity", birth),
            ("url", "http://127.0.0.1:8188"), ("source_revision", "revision"), ("source_content_digest", "content")]:
        (root / name).write_text(value)
    (root / "config.json").write_bytes(config)
    (root / "launch.json").write_text(json.dumps({"launch_token":"nonce", "pid":910001, "url":"http://127.0.0.1:8188", "process_start_identity":birth}))
    reference = {"generation":1,"target":{"pid":910001}}
    broker = SimpleNamespace(state="ready-bound", channel=None, role_authority=SimpleNamespace(reference=lambda:reference))
    graph = gh._NativeEngineGraph.__new__(gh._NativeEngineGraph)
    graph.process = SimpleNamespace(pid=910001, _astrid_process_birth=birth, poll=lambda:0, wait=lambda timeout:0)
    graph.profile = {"engine_endpoint":"http://127.0.0.1:8188", "session_config_digest":digest,
        "engine_launch":{"session_root":str(root),"source_revision":"revision","source_content_digest":"content"}}
    graph.engine = graph.listener = broker
    graph.custody = SimpleNamespace(_handles={910002:(birth,object())})
    graph._clean_ack = None
    graph._listener_exit = {"current_reference":{"target":{"pid":910002}},"generation":1,"exit_code":0}
    monkeypatch.setattr(cb, "default_process_identity", lambda pid:{"parent_pid":910001})
    monkeypatch.setattr(gh, "verify_tree_absent", lambda proc:None)
    calls=[]
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs == dict(capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=2,check=False)
        if phase == "registry": return subprocess.CompletedProcess(argv,0,"fixture :8188 (LISTEN)\n","")
        return subprocess.CompletedProcess(argv,1,"","")
    monkeypatch.setattr(gh.subprocess,"run",run)
    if phase == "registry": assert graph._measure_registry(root,"nonce",910002)["comfy_pid"] == 910002
    else: assert graph.abort()["engine"]["exit_code"] == 0
    expected = ["lsof", *(["-O"] if platform == "darwin" else []), "-nP"]
    expected += ["-a","-p","910002","-iTCP:8188","-sTCP:LISTEN"] if phase == "registry" else ["-t","-iTCP:8188","-sTCP:LISTEN"]
    assert calls == [expected]
