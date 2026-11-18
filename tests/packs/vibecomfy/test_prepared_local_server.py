"""LS-4: admitted templates reach the ordinary owned-output server boundary."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.vibecomfy import local_server_engine as engine
from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401
from vibecomfy.prepared import PreparedWorkflowContext, PreparedWorkflowUnavailable
from vibecomfy.schema.cache import object_info_payload_checksum
from vibecomfy.workflow import VibeEdge, VibeNode, VibeOutput, VibeWorkflow, WorkflowSource
from vibecomfy.workflow_bundle import WorkflowBundle, load_bundle

pytestmark = pytest.mark.integration


def fixture_bundle(*, media=False):
    workflow = VibeWorkflow("ls4-attached", WorkflowSource("ls4-attached"))
    workflow.nodes = {
        "1": VibeNode("1", "LoadImage" if media else "SeedImage", uid="source",
                      inputs={"image": "portrait.png"} if media else {"seed": 42, "asset": "installed.safetensors"},
                      native_input_names=["image"] if media else ["seed", "asset"],
                      native_input_types=["STRING"] if media else ["INT", "STRING"],
                      native_output_names=["IMAGE", "MASK"] if media else ["IMAGE"],
                      native_output_types=["IMAGE", "MASK"] if media else ["IMAGE"]),
        "2": VibeNode("2", "SaveImage", uid="save", inputs={"filename_prefix": "astrid"},
                      native_input_names=["images", "filename_prefix"],
                      native_input_types=["IMAGE", "STRING"],
                      native_output_names=[], native_output_types=[]),
    }
    workflow.edges = [VibeEdge("1", "0", "2", "images")]
    workflow.outputs = [VibeOutput("2", "SaveImage", name="image", artifact_kind="image", mime_type="image/png")]
    if media:
        workflow.register_input("image", "1", "image", "portrait.png", type="STRING", media_semantics="image")
    else:
        workflow.register_input("seed", "1", "seed", 42, type="INT")
    workflow.register_input("prefix", "2", "filename_prefix", "astrid", type="STRING")
    return load_bundle(workflow)


def admit(bundle, values, comfy, tmp_path):
    destination = tmp_path / "admission"
    destination.mkdir()
    provider = engine._live_preflight(bundle, values, comfy.url, destination)
    context = PreparedWorkflowContext(
        source_identity=bundle.revision_id, capability_identity="ls4-test-capability",
        authority_scope="test/project", settings_generation="settings-1",
        installation_generation="installation-1", target_identity=comfy.url,
        target_generation="attached-1", schema_digest=object_info_payload_checksum(comfy.schema),
        compiler_identity="ls4-test-checkout",
    )
    held = engine.prepare_local_workflow(bundle, values, provider, context=context, executable_hooks=False)
    assert held is not None
    return held, context


def forbid_job_preparation(monkeypatch, held):
    def forbidden(*args, **kwargs):
        raise AssertionError("per-job source/compile/schema work")
    for name in ("_live_preflight", "_approve_live", "local_server_bundle"):
        monkeypatch.setattr(engine, name, forbidden)
    monkeypatch.setattr(VibeWorkflow, "compile", forbidden)
    monkeypatch.setattr(WorkflowBundle, "compile", forbidden)
    monkeypatch.setattr(WorkflowBundle, "materialize_ui", forbidden)
    monkeypatch.setattr(held.provider, "get_schema", forbidden)
    monkeypatch.setattr(held.provider, "object_info", forbidden)


def assert_owned_result(result, destination, comfy, index):
    result_path = Path(result.managed_generation_result_path)
    assert result_path.is_file() and result_path.is_relative_to(destination)
    managed = json.loads(result_path.read_text())
    assert managed["task_id"] == "task-1"
    assert len(result.outputs) == 1
    assert len(managed["outputs"]) == 1
    # Runtime's outputs tuple retains the server-reported hint; the durable
    # managed record owns the verified path relative to the attempt receipt.
    output = (result_path.parent / managed["outputs"][0]["path"]).resolve()
    assert output.is_relative_to(destination)
    original = comfy.output / f"astrid_{index + 1}.png"
    assert output.read_bytes() == original.read_bytes()
    assert Path(result.outputs[0]).name == original.name
    assert hashlib.sha256(output.read_bytes()).hexdigest() == managed["outputs"][0]["sha256"]
    assert hashlib.sha256(original.read_bytes()).hexdigest() == comfy.output_hashes[original.name]
    journal = next((destination / ".vibecomfy-runtime").glob("out/runs/*/attempt.json"))
    evidence = json.loads(journal.read_text())
    assert evidence["adapter"]["kind"] == "external"
    assert evidence["artifact_retrieval"]["transport"] == "comfy-http-view"
    assert evidence["artifacts"][0]["download_source"] == "external_http"
    assert evidence["queue_acceptance"]["prompt_id"] == f"ours-{index + 1}"
    assert "lifecycle_retrieval" not in evidence
    return managed


def run_prepared(bundle, held, context, comfy, destination, values, index):
    destination.mkdir(parents=True, exist_ok=True)
    return engine.run_local_server(
        bundle, destination, comfy_url=comfy.url, comfy_output_dir="", task_identity="task-1",
        attempt_identity=f"attempt-{index}", run_inputs=values,
        prepared_workflow=held, prepared_context=context,
    )


def test_two_prepared_jobs_keep_private_prefixes_and_real_output_custody(comfy, tmp_path, monkeypatch):
    bundle = fixture_bundle()
    held, context = admit(bundle, {"seed": 7, "prefix": "user/name"}, comfy, tmp_path)
    schema_reads = sum(path == "/object_info" for _, path, _ in comfy.calls)
    forbid_job_preparation(monkeypatch, held)
    for index, seed in enumerate((11, 22)):
        destination = tmp_path / f"attempt-{index}" / "outputs"
        result = run_prepared(bundle, held, context, comfy, destination,
                              {"seed": seed, "prefix": "user/name"}, index)
        managed = assert_owned_result(result, destination, comfy, index)
        assert managed["inputs"]["seed"] == seed
        assert managed["inputs"]["prefix"] == "user/name"
    assert [prompt["1"]["inputs"]["seed"] for prompt in comfy.prompts] == [11, 22]
    prefixes = [prompt["2"]["inputs"]["filename_prefix"] for prompt in comfy.prompts]
    assert len(set(prefixes)) == 2 and all(value.startswith("user/name_astrid_attempt-") for value in prefixes)
    assert sum(path == "/object_info" for _, path, _ in comfy.calls) == schema_reads
    assert any(path.startswith("/view?") for _, path, _ in comfy.calls)
    assert [(method, path) for method, path, _ in comfy.calls if method == "POST"] == [
        ("POST", "/prompt"), ("POST", "/prompt")]
    assert bundle.workflow.nodes["1"].inputs["seed"] == 42
    assert bundle.workflow.nodes["2"].inputs["filename_prefix"] == "astrid"


def test_prepared_media_slots_receive_actual_uploaded_names(comfy, tmp_path, monkeypatch):
    comfy.schema["LoadImage"] = {
        "input": {"required": {"image": [["server-existing.png"], {"image_upload": True}]}},
        "output": ["IMAGE", "MASK"], "output_name": ["IMAGE", "MASK"],
    }
    bundle = fixture_bundle(media=True)
    held, context = admit(bundle, {"image": "portrait.png"}, comfy, tmp_path)
    forbid_job_preparation(monkeypatch, held)
    staged = []
    for index in range(2):
        destination = tmp_path / f"attempt-{index}" / "outputs"
        inputs = destination / "engine-input"
        inputs.mkdir(parents=True)
        source = inputs / "portrait.png"
        Image.new("RGB", (16, 16), (index, 30, 50)).save(source)
        staged.append(source.read_bytes())
        result = run_prepared(bundle, held, context, comfy, destination, {"image": "portrait.png"}, index)
        assert_owned_result(result, destination, comfy, index)
    uploads = [body for method, path, body in comfy.calls if path == "/upload/image"]
    assert len(uploads) == 2
    assert all(value in body for value, body in zip(staged, uploads, strict=True))
    assert [prompt["1"]["inputs"]["image"] for prompt in comfy.prompts] == [
        "astrid/task-1/uploaded.png", "astrid/task-1/uploaded.png"]
    assert len({prompt["2"]["inputs"]["filename_prefix"] for prompt in comfy.prompts}) == 2


@pytest.mark.parametrize("change", ["source", "generation", "target", "invalidated"])
def test_stale_preparation_refuses_before_upload_or_queue(comfy, tmp_path, change):
    bundle = fixture_bundle()
    held, context = admit(bundle, {"seed": 7}, comfy, tmp_path)
    if change == "source":
        bundle = replace(bundle, revision_id="another-source")
    elif change == "generation":
        context = replace(context, target_generation="another-generation")
    elif change == "target":
        context = replace(context, target_identity="http://127.0.0.1:1")
    else:
        held.session.invalidate()
    before = list(comfy.calls)
    with pytest.raises(PreparedWorkflowUnavailable):
        run_prepared(bundle, held, context, comfy, tmp_path / "attempt" / "outputs", {"seed": 9}, 0)
    assert comfy.calls == before
    assert not comfy.prompts


def test_h3_executable_hooks_keep_the_existing_full_path(monkeypatch):
    import yaml

    contribution = Path(engine.__file__).parents[1] / "h3_av/extensions/vibecomfy.workflows/h3.yaml"
    entry = yaml.safe_load(contribution.read_text())
    assert entry["build"] == "hooks.py:build"
    assert entry["after_outputs"] == "hooks.py:after_outputs"
    def forbidden(*args, **kwargs):
        raise AssertionError("an executable entry must not be frozen")
    monkeypatch.setattr(engine, "_capture_live_schemas", forbidden)
    assert engine.prepare_local_workflow(None, {}, None, context=None, executable_hooks=True) is None


@pytest.mark.parametrize("change", ["additional_input", "omitted_input", "prefix_colon", "prefix_long"])
def test_unusable_prepared_fill_uses_full_path_before_upload(comfy, tmp_path, monkeypatch, change):
    from astrid.core.generation.backends import vibecomfy_local_server as backend

    comfy.schema["LoadImage"] = {
        "input": {"required": {"image": [["server-existing.png"], {"image_upload": True}]}},
        "output": ["IMAGE", "MASK"], "output_name": ["IMAGE", "MASK"],
    }
    bundle = fixture_bundle(media=True)
    admitted = {"image": "portrait.png"}
    values = {"image": "portrait.png", "prefix": "fresh/name"}
    if change == "omitted_input":
        admitted, values = values, admitted
    elif change in {"prefix_colon", "prefix_long"}:
        admitted = {"image": "portrait.png", "prefix": "safe"}
        values["prefix"] = "run:1" if change == "prefix_colon" else "x" * 300
    held, context = admit(bundle, admitted, comfy, tmp_path)
    events = []

    def capture(owner, name, event):
        original = getattr(owner, name)

        def wrapped(*args, **kwargs):
            events.append(event)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapped)

    capture(engine, "_live_preflight", "full-preflight")
    capture(engine, "local_server_bundle", "full-bundle")
    capture(backend, "upload_image", "upload")
    capture(engine, "_approve_live", "full-approval")

    def incompatible_fill(*_args, **_kwargs):
        raise AssertionError("an incompatible prepared fill was used after upload")

    monkeypatch.setattr(type(held.session), "fill", incompatible_fill)
    destination = tmp_path / "attempt" / "outputs"
    source = destination / "engine-input" / "portrait.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), (20, 30, 40)).save(source)
    result = run_prepared(bundle, held, context, comfy, destination, values, 0)
    assert events == ["full-preflight", "full-bundle", "upload", "full-approval"]
    assert_owned_result(result, destination, comfy, 0)
    assert len([1 for method, path, _ in comfy.calls if path == "/upload/image"]) == 1
    assert len(comfy.prompts) == 1
    assert comfy.prompts[0]["1"]["inputs"]["image"] == "astrid/task-1/uploaded.png"
    prefix = values.get("prefix", "astrid")
    assert comfy.prompts[0]["2"]["inputs"]["filename_prefix"] == prefix + "_astrid_attempt-0"


def test_wrapped_compile_stage_admission_refusal_returns_to_full_path(comfy, tmp_path):
    from vibecomfy.workflow import WorkflowCompileError

    bundle = fixture_bundle()
    held, context = admit(bundle, {"seed": 7}, comfy, tmp_path)
    bundle.workflow.inputs["seed"].type = "STRING"
    bundle = load_bundle(bundle.workflow)
    context = replace(context, source_identity=bundle.revision_id)
    values = {"seed": "seven"}
    assert engine.prepare_local_workflow(
        bundle, values, held.provider, context=context, executable_hooks=False,
    ) is None
    with pytest.raises(WorkflowCompileError) as full:
        engine._live_preflight(bundle, values, comfy.url, tmp_path / "full-reference")
    with pytest.raises(WorkflowCompileError) as fallback:
        run_prepared(bundle, None, context, comfy, tmp_path / "attempt" / "outputs", values, 0)
    assert str(fallback.value) == str(full.value)
    assert not [call for call in comfy.calls if call[0] == "POST"]
    assert not comfy.prompts
