"""Injected endpoint route tests. Actual authenticated HTTP awaits pending38."""
from contextlib import contextmanager
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.core.generation.backends.base import BackendAdapter, GenerationResult
from astrid.core.generation.backends.registry import (
    GenerationBackendDescriptor, GenerationBackendRegistry, resolve_workflow_descriptor,
)
from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
from astrid.core.generation.backends.vibecomfy_workflow import VibeComfyWorkflowBackend
from astrid.core.generation.backends.workflow import WorkflowBackendAdapter
from astrid.core.generation.engine_contracts import UnsupportedPlacement
from astrid.core.pack.entrypoint import canonical_runtime_entrypoint
from astrid.packs.vibecomfy import production_engine as engine


RUNPOD = {"target": {"kind": "runpod", "pod_id": "original-pod", "provider_account_ref": "account",
                     "storage": {"network_volume_id": "original-volume"}}}


@pytest.fixture
def action():
    with canonical_runtime_entrypoint("vibecomfy.run"):
        return importlib.import_module("astrid.packs.vibecomfy.actions.run.run")


@pytest.mark.parametrize("placement,execution_request_value,backend", [
    (None, None, "vibecomfy-h3-workflow"), ("local", None, "vibecomfy-h3-workflow"),
    (None, {"target": {"kind": "default"}}, "vibecomfy-h3-workflow"),
    ("runpod", RUNPOD, "vibecomfy-h3-workflow"),
])
def test_pure_registry_selection_keeps_model_defaults(monkeypatch, placement, execution_request_value, backend):
    registry = GenerationBackendRegistry()
    monkeypatch.setattr(GenerationBackendDescriptor, "load_class", Mock(side_effect=AssertionError("import")))
    selected = resolve_workflow_descriptor(registry, placement=placement, execution_request=execution_request_value)
    assert selected.backend_id == backend
    assert registry.get_descriptor("local").placement.default == "local"
    assert registry.get_descriptor("local").init_kwargs == {}
    assert registry.get_descriptor("cloud").placement.default == "local"


@pytest.mark.parametrize("placement,execution_request_value", [
    ("runpod", None), ("local", RUNPOD), ("unsupported", RUNPOD),
    (False, None), (None, {"target": {"kind": "machine", "id": "machine"}}),
])
def test_wrong_placement_precedes_all_action_effects(action, monkeypatch, tmp_path, placement, execution_request_value):
    registry = GenerationBackendRegistry()
    monkeypatch.setattr(GenerationBackendDescriptor, "create", Mock(side_effect=AssertionError("create")))
    stage = Mock(side_effect=AssertionError("stage"))
    monkeypatch.setattr(action, "_stage_managed_asset_manifest", stage)
    endpoint = Mock(side_effect=AssertionError("endpoint"))
    with pytest.raises(UnsupportedPlacement):
        action._run_and_settle(tmp_path / "absent", tmp_path / "out", task_identity="task-1",
                               managed_assets="absent", placement=placement, execution_request=execution_request_value,
                               registry=registry, endpoint_checkout=endpoint)
    stage.assert_not_called()
    endpoint.assert_not_called()
    assert not (tmp_path / "out").exists()


def test_missing_endpoint_seam_fails_before_staging(action, monkeypatch, tmp_path):
    monkeypatch.setattr(action, "_stage_managed_asset_manifest", Mock(side_effect=AssertionError("stage")))
    with pytest.raises(ValueError, match="pending38"):
        action._run_and_settle(tmp_path / "workflow.py", tmp_path / "out", task_identity="task-1",
                               placement="runpod", execution_request=RUNPOD, managed_assets="absent")
    assert not (tmp_path / "out").exists()


def test_local_workflow_optional_api_preserves_result_and_legacy_defaults(monkeypatch, tmp_path):
    result = engine.ProductionRunResult(outputs=(tmp_path / "image.png",))
    produce = Mock(return_value=result)
    monkeypatch.setattr(engine, "run_workflow_result_path", produce)
    backend = GenerationBackendRegistry().create("local")
    assert isinstance(backend, WorkflowBackendAdapter)
    assert backend.workflow(tmp_path / "workflow.py", tmp_path, task_identity="task-1") is result
    produce.assert_called_once_with(tmp_path / "workflow.py", tmp_path, task_identity="task-1",
                                    profile_id="pip_embedded", hc03_profile=None,
                                    expected_execution_identity=None)
    assert VibeComfyWorkflowBackend.generate is VibeComfyBackend.generate
    assert BackendAdapter.__abstractmethods__ == frozenset({"generate"})


def test_model_only_adapter_still_constructs_but_cannot_run_workflow(action, monkeypatch, tmp_path):
    class ModelOnly(BackendAdapter):
        def generate(self, entry, mode, params, out_dir):
            return GenerationResult()
    monkeypatch.setattr(GenerationBackendDescriptor, "load_class", lambda self: ModelOnly)
    with pytest.raises(ValueError, match="does not support workflows"):
        action._run_and_settle(tmp_path / "absent", tmp_path / "out", task_identity="task-1")
    assert not (tmp_path / "out").exists()


@pytest.fixture
def canonical_case(tmp_path, monkeypatch):
    # A real sealed canonical bundle; only the GPU/endpoint boundary is injected.
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle
    monkeypatch.chdir(tmp_path)
    workflow = VibeWorkflow("route-test", WorkflowSource("route-test"))
    workflow.add_node("Integer", _id="1", uid="integer", value=7)
    workflow.register_input("value", "1", "value", value=7, type="INT")
    source = tmp_path / "workflow.py"
    (tmp_path / "source.json").write_text(
        '{"nodes":[{"id":1,"type":"Integer","inputs":[],"widgets_values":[7]}]}'
    )
    emit_bundle(workflow, source, {"operation": "authored"})
    profile = {"vibecomfy_session": {"original_binding": "owner-1"}}
    profile_bytes = json.dumps(profile).encode()
    profile_path = tmp_path / "readiness.json"
    profile_path.write_bytes(profile_bytes)
    loaded = engine.load_workflow_path(source, tmp_path / "preflight")
    return SimpleNamespace(
        workflow=source, profile=profile, profile_path=str(profile_path),
        profile_hash="sha256:" + hashlib.sha256(profile_bytes).hexdigest(),
        identity=engine.loaded_workflow_execution_identity(loaded, profile),
        out=tmp_path / "out",
    )


def producer_result(root, *, task="task-1", attempt="attempt-1"):
    output = root / "outputs" / "final.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"injected-endpoint-media")
    payload = {
        "schema_version": 1, "kind": "managed-generation-result.v1",
        "inputs": {"value": 8}, "outputs": [{
            "producer_output_id": "video:0", "output_port": "video", "ordinal": 0,
            "path": "outputs/final.mp4", "media_type": "video/mp4", "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        }], "created": "2026-10-27T22:00:00Z", "warnings": [],
        "task_id": task, "attempt_id": attempt, "producer_run_id": "producer-1",
        "outcomes": {phase: {"status": status} for phase, status in (
            ("execution", "succeeded"), ("retrieval", "succeeded"),
            ("verification", "succeeded"), ("publication", "not_started"))},
        "evidence": {"producer": {"engine": {"name": "injected-endpoint"}},
                     "transport": {"location": {"binding": "original-owner"}}},
    }
    envelope = root / "managed-generation-result.json"
    envelope.write_text(json.dumps(payload))
    return SimpleNamespace(outputs=(output,), managed_generation_result_path=envelope), payload


@pytest.mark.parametrize("route", ["local", "runpod"])
def test_action_registry_production_and_existing_manifestv2(action, canonical_case, monkeypatch, tmp_path, route):
    case = canonical_case
    produced, payload = producer_result(tmp_path / "producer")
    calls = []
    class Checkout:
        _last_run_result = produced
        def run_compiled_workflow(self, resolved, destination, **kwargs):
            calls.append((resolved, destination, kwargs))
            return produced.outputs
        def cancel(self):
            pytest.fail("unexpected cancel")
        def release(self, **kwargs):
            pytest.fail("unexpected release")
    @contextmanager
    def attachment(**kwargs):
        assert kwargs["hc03_profile"] == case.profile
        assert kwargs["invocation_identity"] == "task-1"
        try:
            yield Checkout()
        finally:
            closed.append(True)
    closed = []
    endpoint = Mock(side_effect=attachment)
    original = Mock(return_value=Checkout())
    monkeypatch.setattr("astrid.core.generation.backends.vibecomfy.CheckoutServerAdapter.from_host_session", original)
    workflow_calls = []
    # Local placement reaches the base workflow through the H3 workflow backend.
    backend_class = VibeComfyWorkflowBackend if route == "runpod" else VibeComfyBackend
    original_workflow = backend_class.workflow
    def tracked_workflow(self, *args, **kwargs):
        workflow_calls.append((args, kwargs))
        return original_workflow(self, *args, **kwargs)
    monkeypatch.setattr(backend_class, "workflow", tracked_workflow)
    registry = GenerationBackendRegistry()
    selection = Mock(wraps=registry.get_descriptor)
    monkeypatch.setattr(registry, "get_descriptor", selection)
    kwargs = dict(
        task_identity="task-1", attempt_identity="attempt-1", execution_identity=case.identity,
        readiness_profile_path=case.profile_path, readiness_profile_hash=case.profile_hash,
        workflow_inputs=' { "value" : 8 } ', registry=registry,
    )
    if route == "runpod":
        kwargs.update(placement="runpod", execution_request=RUNPOD, endpoint_checkout=endpoint)
    manifest = action._run_and_settle(case.workflow, case.out, **kwargs)
    selection.assert_called_once_with("vibecomfy-h3-workflow")
    assert len(workflow_calls) == len(calls) == 1
    assert calls[0][2] == {"task_identity": "task-1", "attempt_identity": "attempt-1", "run_inputs": {"value": 8}}
    if route == "runpod":
        endpoint.assert_called_once()
        original.assert_not_called()
        assert closed == [True]
    else:
        original.assert_called_once()
        endpoint.assert_not_called()
        assert closed == []
    assert manifest["schema_version"] == 2
    media, envelope = manifest["outputs"]
    assert media["output_port"] == "vibecomfy_run"
    assert media["ordinal"] == 0 and media["role"] == "result"
    assert media["content_hash"] == "sha256:" + payload["outputs"][0]["sha256"]
    assert (case.out / media["path"]).read_bytes() == b"injected-endpoint-media"
    assert envelope["output_port"] == "result_manifest" and envelope["role"] == "auxiliary"
    assert envelope["ordinal"] == 1
    assert hashlib.sha256((case.out / envelope["path"]).read_bytes()).hexdigest() == envelope["content_hash"].removeprefix("sha256:")
    assert manifest["managed_generation_result"]["producer_run_id"] == "producer-1"
    assert manifest["managed_generation_result"]["outcomes"]["publication"]["status"] == "not_started"
    assert not list(case.out.rglob("generation-manifest*"))
    assert json.loads((case.out / "manifest.json").read_bytes()) == manifest
    # Compare exact bytes to the existing materializer on the same producer
    # envelope, including media/envelope hashes, ordinals and auxiliary role.
    reference_root = tmp_path / "legacy-materializer"
    reference = action._materialize_managed_generation_result(
        reference_root,
        engine.ProductionRunResult(outputs=produced.outputs,
                                   managed_generation_result_path=produced.managed_generation_result_path),
        task_identity="task-1", attempt_identity="attempt-1", workflow_path=case.workflow,
    )
    assert reference == manifest
    assert (reference_root / "manifest.json").read_bytes() == (case.out / "manifest.json").read_bytes()


@pytest.mark.parametrize("change", ["execution", "input", "profile"])
def test_production_validation_happens_before_endpoint(action, canonical_case, tmp_path, change):
    case = canonical_case
    endpoint = Mock(side_effect=AssertionError("endpoint effect"))
    kwargs = dict(task_identity="task-1", attempt_identity="attempt-1", execution_identity=case.identity,
                  readiness_profile_path=case.profile_path, readiness_profile_hash=case.profile_hash,
                  execution_request=RUNPOD, endpoint_checkout=endpoint)
    if change == "execution":
        kwargs["execution_identity"] = "changed"
    elif change == "input":
        kwargs["workflow_inputs"] = '{"unknown":8}'
    else:
        kwargs["readiness_profile_hash"] = "sha256:" + "0" * 64
    with pytest.raises((ValueError, engine.ProductionEngineError)):
        action._run_and_settle(case.workflow, case.out, **kwargs)
    endpoint.assert_not_called()
    assert not (case.out / "manifest.json").exists()


def test_endpoint_attachment_failure_is_not_retried(action, canonical_case):
    case = canonical_case
    endpoint = Mock(side_effect=RuntimeError("uncertain original endpoint"))
    with pytest.raises(RuntimeError, match="uncertain original endpoint"):
        action._run_and_settle(case.workflow, case.out, task_identity="task-1", attempt_identity="attempt-1",
                               execution_identity=case.identity, readiness_profile_path=case.profile_path,
                               readiness_profile_hash=case.profile_hash, execution_request=RUNPOD,
                               endpoint_checkout=endpoint)
    endpoint.assert_called_once()
    assert not (case.out / "manifest.json").exists()


def test_attached_run_failure_uses_original_control_once_and_closes(action, canonical_case):
    case = canonical_case
    release = Mock()
    run = Mock(side_effect=RuntimeError("producer failed"))
    closed = []
    @contextmanager
    def attachment(**kwargs):
        try:
            yield SimpleNamespace(run_compiled_workflow=run, release=release, cancel=Mock())
        finally:
            closed.append(True)
    endpoint = Mock(side_effect=attachment)
    with pytest.raises(RuntimeError, match="producer failed"):
        action._run_and_settle(case.workflow, case.out, task_identity="task-1", attempt_identity="attempt-1",
                               execution_identity=case.identity, readiness_profile_path=case.profile_path,
                               readiness_profile_hash=case.profile_hash, execution_request=RUNPOD,
                               endpoint_checkout=endpoint)
    endpoint.assert_called_once()
    run.assert_called_once()
    release.assert_called_once_with(reason="failed")
    assert closed == [True]
    assert not (case.out / "manifest.json").exists()


def test_workflow_backend_local_mode_calls_local_server_with_endpoint_record(monkeypatch, tmp_path):
    result = engine.ProductionRunResult(outputs=(tmp_path / "image.png",))
    produce = Mock(return_value=result)
    monkeypatch.setattr(engine, "run_workflow_result_path", produce)
    backend = VibeComfyWorkflowBackend()
    got = backend.workflow(
        tmp_path / "workflow.py", tmp_path / "out", task_identity="task-1", attempt_identity="a-1",
        profile_id="local_server", comfy_url="http://127.0.0.1:8188", comfy_output_dir=str(tmp_path / "output"),
    )
    assert got is result
    args, kwargs = produce.call_args
    assert args == (tmp_path / "workflow.py", tmp_path / "out")
    assert kwargs["profile_id"] == "local_server"
    assert kwargs["comfy_url"] == "http://127.0.0.1:8188"
    assert kwargs["comfy_output_dir"] == str(tmp_path / "output")
    assert "endpoint_checkout" not in kwargs


def test_workflow_backend_local_mode_requires_endpoint_fields(tmp_path):
    with pytest.raises(engine.ProductionEngineError, match="requires comfy_url"):
        VibeComfyWorkflowBackend().workflow(
            tmp_path / "w.py", tmp_path / "out", task_identity="t", profile_id="local_server")


def test_workflow_backend_attached_mode_requires_no_shared_output_directory(monkeypatch, tmp_path):
    result = engine.ProductionRunResult(outputs=(tmp_path / "image.png",))
    produce = Mock(return_value=result)
    monkeypatch.setattr(engine, "run_workflow_result_path", produce)
    assert VibeComfyWorkflowBackend().workflow(
        tmp_path / "w.py", tmp_path / "out", task_identity="t", profile_id="local_server",
        comfy_url="http://127.0.0.1:8188",
    ) is result
    assert "comfy_output_dir" not in produce.call_args.kwargs


def test_resolver_selects_h3_backend_for_engine_bound_workflows_and_refuses_stray_runpod_targets():
    registry = GenerationBackendRegistry()
    descriptor = registry.get_descriptor("vibecomfy-h3-workflow")
    assert descriptor.placement.supported == ("local", "runpod")
    assert descriptor.placement.default == "local" and descriptor.placement.engine == "vibecomfy-h3"
    for placement in (None, "local"):
        assert resolve_workflow_descriptor(registry, placement=placement).backend_id == "vibecomfy-h3-workflow"
    assert resolve_workflow_descriptor(registry, placement="runpod", execution_request=RUNPOD).placement.resolve("runpod") == "runpod"
    for placement in (None, "local"):
        with pytest.raises(UnsupportedPlacement):
            resolve_workflow_descriptor(registry, placement=placement, execution_request=RUNPOD)
    with pytest.raises(UnsupportedPlacement):
        resolve_workflow_descriptor(registry, placement="runpod")
