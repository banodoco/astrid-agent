from __future__ import annotations

import importlib


def test_direct_local_run_routes_through_owned_preparation(tmp_path, monkeypatch):
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    workflow_route = importlib.import_module("astrid.packs.vibecomfy.workflow_route")
    local_job = importlib.import_module("astrid.packs.vibecomfy.local_job")
    managed_preparation = importlib.import_module("astrid.core.generation.managed_preparation")
    engine_endpoint = importlib.import_module("astrid.core.generation.engine_endpoint")
    local_endpoint = importlib.import_module("astrid.core.generation.engine_local_endpoint")
    invocation_preflight = importlib.import_module("astrid.packs.vibecomfy.invocation_preflight")
    from types import SimpleNamespace

    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}")
    output = tmp_path / "outputs"
    monkeypatch.setattr(action, "_evidence_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(workflow_route, "WorkflowRunner", lambda: object())
    monkeypatch.setattr(engine_endpoint, "read_readiness_profile", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(local_endpoint, "default_engine_root", lambda _engine: str(tmp_path / "managed-engine"))
    monkeypatch.setattr(managed_preparation, "read_preparation", lambda **_kwargs: None)
    monkeypatch.setattr(action, "_t9_child_dispatch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(action, "_stage_managed_asset_manifest",
                        lambda *_args, **_kwargs: SimpleNamespace(manifest={"assets": []}))
    monkeypatch.setattr(action, "resolve_inputs",
                        lambda _manifest, bindings: {**bindings, "asset_input": "asset.png"})
    monkeypatch.setattr(action, "_stage_source_video", lambda *_args, **_kwargs: "source.mp4")
    monkeypatch.setattr(invocation_preflight, "preflight_invocation", lambda *_args, **_kwargs: None)
    calls = []

    def run_owned_action(workflow_path, output_root, **kwargs):
        calls.append((workflow_path, output_root, kwargs))
        return {"routed": True}

    monkeypatch.setattr(local_job, "run_owned_action", run_owned_action)
    result = action._run_and_settle(
        workflow,
        output,
        task_identity="task-local-fallback",
        execution_identity="execution-local-fallback",
        workflow_inputs='{"seed": 7}',
        managed_assets="assets.zip",
        source_video="source.mp4",
        source_video_node="42",
        source_video_widget="video",
        readiness_profile_path="profile.json",
        readiness_profile_hash="profile-hash",
        manifest={"engine": "comfyui"},
        align="check",
    )

    assert result == {"routed": True}
    assert len(calls) == 1 and calls[0][:2] == (workflow, output)
    routed = calls[0][2]
    generated_attempt = routed.pop("attempt_identity")
    assert isinstance(generated_attempt, str) and len(generated_attempt) == 32
    assert routed == {
        "task_identity": "task-local-fallback",
        "expected_execution_identity": "execution-local-fallback",
        "align": "check",
        "workflow_inputs": '{"asset_input": "asset.png", "seed": 7, "source_video": "source.mp4"}',
        "execution_request": None,
        "manifest": {"engine": "comfyui", "models_root": str(tmp_path / "managed-engine" / "models")},
        "entry": None,
        "after_outputs_job": "{}",
    }


def test_owned_local_run_refuses_a_different_admitted_execution_identity(tmp_path, monkeypatch):
    import pytest

    from types import SimpleNamespace
    from astrid.core.generation import managed_preparation
    from astrid.core.generation.target_inventory import POD_ALIGN
    from astrid.core.generation.local_job import JobInputError
    from astrid.core.generation.target_inventory import POD_STOP
    from astrid.packs.vibecomfy import local_job, production_engine
    pack_api = importlib.import_module("astrid.pack_api")

    workflow = tmp_path / "workflow.py"
    workflow.write_text("pass")
    workflow.with_suffix(".vibe.json").write_text("{}")
    workflow.with_name("source.json").write_text("{}")
    monkeypatch.setattr(pack_api, "canonical_runtime_entrypoint", lambda *_args: _null_scope())
    monkeypatch.setattr(production_engine, "load_workflow_path", lambda *_args: SimpleNamespace(resolved=object()))
    monkeypatch.setattr(production_engine, "_canonical_bundle_value", lambda *_args: SimpleNamespace(workflow=object()))
    monkeypatch.setattr(production_engine, "loaded_workflow_execution_identity", lambda *_args: "actual-identity")
    monkeypatch.setattr(managed_preparation, "ManagedEnginePreparation",
                        lambda **_kwargs: pytest.fail("preparation must not start for a mismatched identity"))

    prior_align = POD_ALIGN.set("trust")
    try:
        with pytest.raises(JobInputError, match="execution identity differs"):
            local_job.run_owned_action(
                workflow,
                tmp_path / "outputs",
                task_identity="task-local-fallback",
                attempt_identity="attempt-local-fallback",
                align="check",
                expected_execution_identity="admitted-identity",
            )
        assert POD_ALIGN.get() == "trust"
    finally:
        POD_ALIGN.reset(prior_align)
    assert POD_STOP.get() is None


def _null_scope():
    from contextlib import nullcontext

    return nullcontext()
