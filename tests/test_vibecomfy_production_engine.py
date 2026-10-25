from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.packs.vibecomfy import production_engine


def test_execution_identity_digest_is_stable_and_model_bound() -> None:
    first = production_engine.execution_identity_digest(
        "model-a", "image/a", model_digest="sha256:" + "a" * 64
    )
    second = production_engine.execution_identity_digest(
        "model-b", "image/a", model_digest="sha256:" + "a" * 64
    )
    assert first != second
    assert first == production_engine.execution_identity_digest(
        "model-a", "image/a", model_digest="sha256:" + "a" * 64
    )


def test_session_identity_ignores_workflow_identity_but_binds_model_bytes() -> None:
    first = production_engine.session_identity_digest(
        "model-a", model_digest="sha256:" + "a" * 64
    )
    same_model_other_workflow = production_engine.session_identity_digest(
        "model-a", model_digest="sha256:" + "a" * 64
    )
    changed_model = production_engine.session_identity_digest(
        "model-b", model_digest="sha256:" + "a" * 64
    )
    changed_bytes = production_engine.session_identity_digest(
        "model-a", model_digest="sha256:" + "b" * 64
    )

    assert same_model_other_workflow == first
    assert changed_model != first
    assert changed_bytes != first


def test_session_identity_binds_verified_auxiliary_runtime_and_config_identity() -> None:
    base = {
        "verified_facts_digest": "sha256:" + "1" * 64,
        "runtime_instance_id": "runtime-a",
        "source_revision": "revision-a",
        "source_content_digest": "sha256:" + "2" * 64,
        "config_digest": "sha256:" + "3" * 64,
        "server_url": "http://gpu.example.test:8188",
    }
    first = production_engine.session_identity_digest(
        "model-a", model_digest="sha256:" + "a" * 64, required_identity=base
    )
    for field, changed in (
        ("verified_facts_digest", "sha256:" + "4" * 64),
        ("config_digest", "sha256:" + "5" * 64),
        ("source_content_digest", "sha256:" + "6" * 64),
        ("runtime_instance_id", "runtime-b"),
    ):
        candidate = dict(base)
        candidate[field] = changed
        assert production_engine.session_identity_digest(
            "model-a", model_digest="sha256:" + "a" * 64, required_identity=candidate
        ) != first


def test_loaded_workflow_session_requirements_include_auxiliary_assets_and_loader_settings() -> None:
    workflow = SimpleNamespace(
        metadata={
            "model_assets": [{"name": "vae.safetensors", "sha256": "a" * 64}],
            "comfy_configuration": {"cache_none": True},
            "loader_settings": {"dtype": "bf16"},
            "requirements": {"models": ["vae.safetensors"], "runtime": {"comfy_version": "==0.26.0"}},
        }
    )
    loaded = production_engine.LoadedWorkflow(
        resolved=SimpleNamespace(workflow=workflow),
        model_id="model-a",
        template_id="workflow-a",
        workflow_identity="workflow-a",
        workflow_revision="revision-a",
        workflow_content_digest="sha256:" + "b" * 64,
    )

    identity = production_engine.loaded_workflow_session_requirements(loaded)

    assert identity["model_id"] == "model-a"
    assert identity["resident_metadata"]["model_assets"][0]["name"] == "vae.safetensors"
    assert identity["resident_metadata"]["comfy_configuration"]["cache_none"] is True
    assert identity["resident_metadata"]["loader_settings"]["dtype"] == "bf16"


def test_loaded_identity_is_bound_to_canonical_metadata_and_content() -> None:
    base = production_engine.LoadedWorkflow(
        resolved=object(),
        model_id="model-a",
        template_id="image/a",
        workflow_identity="portrait",
        workflow_revision="revision-a",
        workflow_content_digest="sha256:" + "b" * 64,
    )
    changed = production_engine.LoadedWorkflow(
        resolved=object(),
        model_id=base.model_id,
        template_id=base.template_id,
        workflow_identity=base.workflow_identity,
        workflow_revision=base.workflow_revision,
        workflow_content_digest="sha256:" + "c" * 64,
    )

    assert production_engine.loaded_workflow_execution_identity(
        base
    ) != production_engine.loaded_workflow_execution_identity(changed)


def test_load_workflow_path_unions_ready_template_and_ui_canonicalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "ready.json"
    ready.write_text('{"template_id":"image/a","bindings":{}}', encoding="utf-8")
    ui = tmp_path / "ui.json"
    ui.write_text('{"nodes":[],"links":[]}', encoding="utf-8")
    ready_workflow = object()
    ui_workflow = object()
    ready_bundle = SimpleNamespace(
        workflow=SimpleNamespace(
            metadata={"ready_template": "image/a", "model_id": "model-a"}
        ),
        workflow_identity="ready-identity",
        revision_id="ready-revision",
        require_canonical_authority=Mock(),
    )
    ui_bundle = SimpleNamespace(
        workflow=SimpleNamespace(metadata={}),
        workflow_identity="ui-identity",
        revision_id="ui-revision",
        require_canonical_authority=Mock(),
    )
    monkeypatch.setattr(
        production_engine,
        "_load_workflow",
        Mock(return_value=ready_workflow),
    )
    canonicalize = Mock(return_value=ui_workflow)
    monkeypatch.setattr(production_engine, "_canonicalize_ui_workflow", canonicalize)
    monkeypatch.setattr(
        production_engine,
        "_canonical_bundle_value",
        lambda value: ready_bundle if value is ready_workflow else ui_bundle,
    )

    loaded_ready = production_engine.load_workflow_path(ready, tmp_path / "ready-scratch")
    loaded_ui = production_engine.load_workflow_path(ui, tmp_path / "ui-scratch")

    assert (loaded_ready.model_id, loaded_ready.template_id) == ("model-a", "image/a")
    assert loaded_ready.workflow_revision == "ready-revision"
    assert (loaded_ui.model_id, loaded_ui.template_id) == ("vibecomfy", "ui-identity")
    assert loaded_ui.workflow_revision == "ui-revision"
    canonicalize.assert_called_once_with(ui.resolve(), {"nodes": [], "links": []}, (tmp_path / "ui-scratch").resolve(),
                                         schema_url="")


def test_canonical_bundle_compiles_through_single_production_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = object()
    bundle = SimpleNamespace(compile=Mock(return_value=approved))
    monkeypatch.setattr(production_engine, "_canonical_bundle_value", lambda _: bundle)

    assert production_engine._canonical_bundle(object()) == (approved, bundle)
    bundle.compile.assert_called_once_with()


def test_workflow_input_binding_validates_public_input_without_mutating_bundle() -> None:
    workflow = SimpleNamespace(inputs={"source_video": SimpleNamespace()})
    bundle = SimpleNamespace(workflow=workflow, require_canonical_authority=Mock())
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(production_engine, "_canonical_bundle_value", lambda _: bundle)
        assert production_engine._validate_workflow_input_bindings(
            bundle,
            {"source_video": "morpheus-speaking.mp4"},
        ) == {"source_video": "morpheus-speaking.mp4"}
    finally:
        monkeypatch.undo()


def test_run_workflow_path_rejects_execution_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = tmp_path / "workflow.json"
    workflow.write_text('{"template_id":"image/a","bindings":{}}', encoding="utf-8")
    monkeypatch.setattr(
        production_engine,
        "load_workflow_path",
        lambda *_args, **_kwargs: production_engine.LoadedWorkflow(
            resolved=SimpleNamespace(metadata={}),
            model_id="model-a",
            template_id="image/a",
            workflow_identity="portrait",
            workflow_revision="revision-a",
            workflow_content_digest="sha256:" + "b" * 64,
        ),
    )
    with pytest.raises(
        production_engine.ProductionEngineError,
        match="execution identity changed",
    ):
        production_engine.run_workflow_path(
            workflow,
            tmp_path / "out",
            task_identity="task-1",
            expected_execution_identity="wrong",
        )


def test_run_executor_verifies_checkout_readiness_pair_and_selects_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    workflow = tmp_path / "workflow.json"
    workflow.write_text('{"template_id":"image/a","bindings":{}}', encoding="utf-8")
    output_root = tmp_path / "outputs"
    engine_output = output_root / "engine-output" / "image.png"
    engine_output.parent.mkdir(parents=True)
    engine_output.write_bytes(b"image")
    readiness = tmp_path / "readiness.json"
    readiness_bytes = json.dumps({"vibecomfy_session": {}}).encode("utf-8")
    readiness.write_bytes(readiness_bytes)
    readiness_hash = "sha256:" + hashlib.sha256(readiness_bytes).hexdigest()
    managed_run = Mock(return_value=(engine_output,))
    monkeypatch.setattr(production_engine, "run_workflow_path", managed_run)

    run._run_and_settle(
        workflow,
        output_root,
        task_identity="task-1",
        execution_identity="execution-1",
        readiness_profile_path=str(readiness),
        readiness_profile_hash=readiness_hash,
    )

    assert managed_run.call_args.kwargs["profile_id"] == "checkout_server"
    assert managed_run.call_args.kwargs["hc03_profile"] == {
        "vibecomfy_session": {}
    }
    with pytest.raises(ValueError, match="must be supplied together"):
        run._run_and_settle(
            workflow,
            tmp_path / "pair-failure",
            task_identity="task-1",
            readiness_profile_path=str(readiness),
            readiness_profile_hash="-",
        )


def test_run_executor_rejects_outputs_outside_private_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    workflow = tmp_path / "workflow.json"
    workflow.write_text('{"template_id":"image/a","bindings":{}}', encoding="utf-8")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"image")
    monkeypatch.setattr(
        production_engine,
        "run_workflow_path",
        Mock(return_value=(outside,)),
    )

    with pytest.raises(FileNotFoundError, match="escaped output custody"):
        run._run_and_settle(
            workflow,
            tmp_path / "outputs",
            task_identity="task-1",
        )


def _managed_result_fixture(tmp_path: Path, *, task_id: str = "task-1") -> tuple[Path, dict]:
    producer_root = tmp_path / "producer-run"
    output = producer_root / "outputs" / "final.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"managed-video")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    payload = {
        "schema_version": 1,
        "kind": "managed-generation-result.v1",
        "inputs": {"prompt": "generalized"},
        "outputs": [{
            "producer_output_id": "vibecomfy:video:0",
            "output_port": "video",
            "ordinal": 0,
            "path": "outputs/final.mp4",
            "media_type": "video/mp4",
            "bytes": output.stat().st_size,
            "sha256": digest,
        }],
        "created": "2026-09-21T12:00:00Z",
        "warnings": [],
        "task_id": task_id,
        "attempt_id": "attempt-1",
        "producer_run_id": "producer-1",
        "outcomes": {
            "execution": {"status": "succeeded"},
            "retrieval": {"status": "succeeded"},
            "verification": {"status": "succeeded"},
            "publication": {"status": "not_started"},
        },
        "evidence": {
            "producer": {"engine": {"name": "vibecomfy"}},
            "transport": {"location": {"opaque": True}},
        },
    }
    envelope = producer_root / "managed-generation-result.json"
    envelope.write_text(json.dumps(payload), encoding="utf-8")
    return envelope, payload


def test_managed_result_is_rebased_and_keeps_producer_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    envelope, payload = _managed_result_fixture(tmp_path)
    production = production_engine.ProductionRunResult(
        outputs=(envelope.parent / "outputs" / "final.mp4",),
        managed_generation_result_path=envelope,
        managed_generation_result=payload,
    )

    manifest = run._materialize_managed_generation_result(
        tmp_path / "host-spool",
        production,
        task_identity="task-1",
        workflow_path=tmp_path / "workflow.py",
    )

    output = manifest["outputs"][0]
    assert output["name"] == "vibecomfy_run"
    assert output["ordinal"] == 0
    assert output["output_port"] == "vibecomfy_run"
    assert output["producer"]["output_port"] == "video"
    assert output["producer"]["media_type"] == "video/mp4"
    assert output["content_hash"].startswith("sha256:")
    assert manifest["managed_generation_result_path"] == (
        "managed-generation/managed-generation-result.json"
    )
    assert manifest["managed_generation_result"]["outputs"][0]["path"] == (
        "outputs/0000-video.mp4"
    )
    assert manifest["outputs"][0]["path"] == "outputs/0000-video.mp4"
    receipt = next(item for item in manifest["outputs"] if item["name"] == "result_manifest")
    assert receipt["path"] == "outputs/managed-generation-result.json"
    assert receipt["media_type"] == "application/json"
    assert (tmp_path / "host-spool" / "outputs" / "0000-video.mp4").read_bytes() == b"managed-video"
    assert (tmp_path / "host-spool" / "outputs" / "managed-generation-result.json").is_file()


def test_managed_result_from_previous_attempt_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    envelope, payload = _managed_result_fixture(tmp_path)
    production = production_engine.ProductionRunResult(
        outputs=(),
        managed_generation_result_path=envelope,
        managed_generation_result=payload,
    )

    with pytest.raises(ValueError, match="attempt_id"):
        run._materialize_managed_generation_result(
            tmp_path / "host-spool",
            production,
            task_identity="task-1",
            attempt_identity="attempt-2",
            workflow_path=tmp_path / "workflow.py",
        )


@pytest.mark.parametrize("mutation", [
    lambda payload: payload.update({"task_id": "stale-task"}),
    lambda payload: payload["outputs"][0].update({"sha256": "0" * 64}),
    lambda payload: payload["outcomes"]["verification"].update({"status": "failed"}),
])
def test_managed_result_invalid_or_late_completion_fails_closed(
    tmp_path: Path, mutation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    envelope, payload = _managed_result_fixture(tmp_path)
    mutation(payload)
    envelope.write_text(json.dumps(payload), encoding="utf-8")
    production = production_engine.ProductionRunResult(
        outputs=(),
        managed_generation_result_path=envelope,
        managed_generation_result=payload,
    )

    with pytest.raises(ValueError, match="managed-generation result"):
        run._materialize_managed_generation_result(
            tmp_path / "host-spool",
            production,
            task_identity="task-1",
            workflow_path=tmp_path / "workflow.py",
        )


@pytest.fixture
def _isolated_comfy_imports() -> Iterator[None]:
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "comfy" or name.startswith("comfy.")
    }
    original_path = list(sys.path)
    for name in original_modules:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name in tuple(sys.modules):
            if name == "comfy" or name.startswith("comfy."):
                sys.modules.pop(name, None)
        sys.modules.update(original_modules)
        sys.path[:] = original_path


def test_pip_embedded_fails_closed_when_embedded_client_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_comfy_imports: None,
) -> None:
    monkeypatch.delenv("COMFYUI_PATH", raising=False)

    with pytest.raises(
        production_engine.ProductionEngineError,
        match=r"set COMFYUI_PATH, or use local_server mode \(comfy_url\) with your own ComfyUI",
    ):
        production_engine._run_profile(
            object(),
            "pip_embedded",
            None,
            model_id="z_image_turbo",
            template_id="image/z_image",
            task_identity="test-task",
            destination=tmp_path / "outputs",
        )


def test_pip_embedded_inserts_tree_containing_embedded_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_comfy_imports: None,
) -> None:
    root = tmp_path / "comfyui-embedded"
    client = root / "comfy" / "client" / "embedded_comfy_client.py"
    client.parent.mkdir(parents=True)
    (root / "comfy" / "__init__.py").write_text("", encoding="utf-8")
    (client.parent / "__init__.py").write_text("", encoding="utf-8")
    client.write_text("class Comfy:\n    pass\n", encoding="utf-8")
    monkeypatch.setenv("COMFYUI_PATH", str(root))

    selected = production_engine._bootstrap_embedded_comfy_client()

    assert selected == root.resolve()
    assert sys.path[0] == str(root.resolve())
    imported = sys.modules["comfy.client.embedded_comfy_client"]
    assert imported.__file__ is not None
    assert Path(imported.__file__).resolve() == client.resolve()


@pytest.mark.integration
def test_pip_embedded_consumes_canonical_bundle_and_pinned_session_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_comfy_imports: None,
) -> None:
    root = tmp_path / "comfyui-embedded"
    client = root / "comfy" / "client" / "embedded_comfy_client.py"
    client.parent.mkdir(parents=True)
    (root / "comfy" / "__init__.py").write_text("", encoding="utf-8")
    (client.parent / "__init__.py").write_text("", encoding="utf-8")
    client.write_text("class Comfy:\n    pass\n", encoding="utf-8")
    monkeypatch.setenv("COMFYUI_PATH", str(root))

    workflow = SimpleNamespace(metadata={"comfy_configuration": {"cache_none": True}})
    bundle = SimpleNamespace(workflow=workflow)
    record = object()
    monkeypatch.setattr(
        production_engine, "_canonical_bundle", lambda _resolved: (record, bundle)
    )
    runtime_run = importlib.import_module("vibecomfy.runtime.run")

    output = tmp_path / "engine-output" / "image.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"image")
    run_embedded_sync = Mock(return_value=SimpleNamespace(outputs=[output]))
    monkeypatch.setattr(runtime_run, "run_embedded_sync", run_embedded_sync)

    result = production_engine._run_profile(
        object(),
        "pip_embedded",
        None,
        model_id="z-image",
        template_id="image/z_image",
        task_identity="task-1",
        destination=tmp_path / "task-out",
    )

    assert result == (output.resolve(),)
    assert run_embedded_sync.call_args.args == (record, bundle)
    config = run_embedded_sync.call_args.kwargs["config"]
    assert config.cache_policy == "none"
    assert config.extra["base_directory"] == str(root.resolve())
    assert config.extra["disable_known_models"] is True
    assert config.extra["extra_model_paths_config"] == [
        str((root / "extra_model_paths.yaml").resolve())
    ]
    assert config.runtime_root == (tmp_path / "task-out" / ".vibecomfy-runtime").resolve()
    assert config.cwd == root.resolve()


@pytest.mark.parametrize("extra", [
    {"source_video": "source.mp4"},
    {"readiness_profile_path": "/unused/profile.json", "readiness_profile_hash": "sha256:unused"},
])
def test_local_server_rejects_incompatible_modes_before_execution(tmp_path, monkeypatch, extra):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    with pytest.raises(ValueError, match="readiness profile or source_video"):
        action._run_and_settle(
            tmp_path / "workflow.json", tmp_path / "attempt" / "outputs",
            task_identity="task-1", comfy_url="http://localhost:43217", **extra,
        )


def test_local_server_mode_selection_canonicalizes_url_and_requires_managed_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    run = Mock(return_value=production_engine.ProductionRunResult(outputs=()))
    monkeypatch.setattr(production_engine, "run_workflow_result_path", run)
    with pytest.raises(ValueError, match="managed_generation_result_path"):
        action._run_and_settle(
            tmp_path / "workflow.json", tmp_path / "attempt" / "outputs",
            task_identity="task-1", comfy_url="http://localhost:43217",
        )
    assert run.call_args.kwargs["profile_id"] == "local_server"
    assert run.call_args.kwargs["comfy_url"] == "http://127.0.0.1:43217"
    assert "comfy_output_dir" not in run.call_args.kwargs


def test_unknown_workflow_input_lists_valid_names(monkeypatch):
    bundle = SimpleNamespace(workflow=SimpleNamespace(inputs={"seed": SimpleNamespace(), "prompt": SimpleNamespace()}))
    monkeypatch.setattr(production_engine, "_canonical_bundle_value", lambda _: bundle)
    with pytest.raises(production_engine.ProductionEngineError, match="valid inputs: prompt, seed"):
        production_engine._validate_workflow_input_bindings(bundle, {"typo": "value"})


def test_local_server_failure_has_runnable_discovery_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}")
    monkeypatch.setattr(
        action, "_run_and_settle",
        Mock(side_effect=production_engine.ProductionEngineError("missing nodes: MissingNode")),
    )

    assert action.main([
        "run", str(workflow), "--out", str(tmp_path / "out"),
        "--comfy-url", "http://127.0.0.1:8188",
    ]) == 1

    error = capsys.readouterr().err
    assert "missing nodes: MissingNode" in error
    assert "recovery: python3 -m astrid.packs.vibecomfy.local scan" in error
    assert "astrid executors" not in error
