"""Private producer freezes complete values without Runtime or provider traffic."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.core.generation import preflight
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.core.generation.model_root import (
    ModelRootBindingError, _canonical_inventory_digest, validate_model_root_binding,
)
from astrid.sdk import generation
from astrid.sdk.exceptions import CapabilityValidationError
from tests.core.generation.backends.test_comfy_model_resolution import target_info


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@pytest.fixture
def producer(monkeypatch, tmp_path):
    info = target_info(vaes=("ae.safetensors",))
    client = ComfyTargetClient(ComfyTarget("http://127.0.0.1:8188", "attached"))
    client.object_info = Mock(return_value=info)
    client.request_json = Mock(side_effect=AssertionError("unexpected endpoint operation"))
    client.submit_prompt = Mock(side_effect=AssertionError("provider invoked"))
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", Mock(side_effect=AssertionError("installation probed")))
    monkeypatch.setattr(generation.GenerationFacade, "workflow", Mock(side_effect=AssertionError("task admitted")))
    monkeypatch.setattr(generation.GenerationFacade, "_invoke", Mock(side_effect=AssertionError("action invoked")))
    root = tmp_path / "models"
    root.mkdir()
    entries = []
    for subdir, name in (("diffusion_models", "z_image_turbo_bf16.safetensors"),
                         ("text_encoders", "qwen_3_4b.safetensors"), ("vae", "ae.safetensors")):
        path = root / subdir / name
        path.parent.mkdir(exist_ok=True)
        data = ("fixture:" + name).encode()
        path.write_bytes(data)
        entries.append({"subdir": subdir, "name": name, "size": len(data),
                        "sha256": "sha256:" + hashlib.sha256(data).hexdigest()})
    entries.sort(key=lambda item: (item["subdir"], item["name"]))
    binding = validate_model_root_binding({"schema_version": 1, "path": str(root), "inventory": entries,
                                         "inventory_digest": _canonical_inventory_digest(entries),
                                         "qualification_identity": None})
    return client, binding


def prepare(producer, inputs=None):
    client, root = producer
    return generation._prepare_verified_image_recipe(
        inputs=inputs or {"prompt": "a paper boat", "seed": 42}, target_client=client, model_root=root,
    )


def test_complete_frozen_recipe_matches_members_and_detached_approval(producer, tmp_path, monkeypatch):
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import ApprovedProjectionRecord, WorkflowBundle, load_bundle

    compiled = []
    original = WorkflowBundle.compile

    def compile_bundle(self, *args, **kwargs):
        compiled.append(kwargs.get("models_root"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(WorkflowBundle, "compile", compile_bundle)
    raw = prepare(producer, {"prompt": "a paper boat", "seed": 42, "size": "512x384", "steps": 4})
    recipe = json.loads(raw)
    assert raw == canonical(recipe)
    assert recipe["entry"] == "z-image"
    assert recipe["resolved_randomness"] == {"seed": 42}
    assert recipe["control_bindings"]["size"] == ["width", "height"]
    assert compiled == [producer[1].path]
    assert set(recipe["members"]) == {"workflow.py", "workflow.vibe.json", "source.json"}
    output = tmp_path / "retained"
    output.mkdir()
    for name, member in recipe["members"].items():
        data = base64.b64decode(member["bytes_base64"], validate=True)
        assert len(data) == member["size"]
        assert "sha256:" + hashlib.sha256(data).hexdigest() == member["sha256"]
        (output / name).write_bytes(data)
    approval = ApprovedProjectionRecord.from_dict(recipe["approval"])
    bundle = load_bundle(output / "workflow.py", trust=Provenance.USER_CONFIRMED)
    assert approval.revision_id == bundle.revision_id
    fresh_api = bundle.workflow.compile("api", variant=approval.selected_variant,
                                        run_inputs=dict(approval.input_binding))
    approval.assert_matches(
        bundle, api_projection=fresh_api,
        ui_projection=json.loads((output / "source.json").read_bytes()),
    )
    api = recipe["approval"]["api_projection"]
    sampler = next(node["inputs"] for node in api.values() if node["class_type"] == "KSampler")
    assert sampler["seed"] == 42 and sampler["steps"] == 4
    assert api[sampler["positive"][0]]["inputs"]["text"] == "a paper boat"
    assert recipe["workflow_inputs"]["width"]["value"] == 512
    assert recipe["workflow_inputs"]["height"]["value"] == 384
    assert len(recipe["model_references"]) == 3
    assert all(reference["inventory"] in recipe["model_root"]["inventory"] for reference in recipe["model_references"])
    producer[0].submit_prompt.assert_not_called()
    producer[0].request_json.assert_not_called()
    generation.GenerationFacade.workflow.assert_not_called()
    generation.GenerationFacade._invoke.assert_not_called()


def test_randomness_is_resolved_before_binding_and_no_files_are_reopened_after_freeze(producer, monkeypatch):
    monkeypatch.setattr("secrets.randbits", lambda bits: 12345)
    frozen = prepare(producer, {"prompt": "one tree"})
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=AssertionError("frozen recipe reopened a path")))
    recipe = json.loads(frozen)
    assert recipe["effective_inputs"]["seed"] == recipe["resolved_randomness"]["seed"] == 12345
    sampler = next(node["inputs"] for node in recipe["approval"]["api_projection"].values() if node["class_type"] == "KSampler")
    assert sampler["seed"] == 12345


@pytest.mark.parametrize("change", [
    {"model": "z-image"}, {"entry": "z-image"}, {"models_root": "/elsewhere"},
    {"project_root": "/elsewhere"}, {"workflow": "print('caller code')"},
    {"trusted": True}, {"seed": True}, {"seed": -2}, {"seed": 2**64},
    {"count": 2}, {"steps": 0}, {"guidance_scale": float("nan")},
    {"size": "automatic"}, {"prompt": ""}, {"image_ref": {"digest": "caller"}},
])
def test_invalid_or_authority_bearing_creative_inputs_fail_before_resolution(producer, change):
    with pytest.raises(CapabilityValidationError):
        prepare(producer, {"prompt": "one tree", **change})
    producer[0].object_info.assert_not_called()
    generation.GenerationFacade.workflow.assert_not_called()


def test_changed_model_bytes_fail_before_resolution(producer):
    client, root = producer
    (root.path / "vae" / "ae.safetensors").write_bytes(b"changed model bytes")
    with pytest.raises(ModelRootBindingError):
        prepare(producer)
    client.object_info.assert_not_called()


def test_target_model_missing_from_actual_root_fails_closed(producer):
    from astrid.core.generation.model_root import ModelRootBinding

    client, root = producer
    path = root.path / "vae" / "ae.safetensors"
    path.unlink()
    inventory = [item for item in root.inventory if item["name"] != "ae.safetensors"]
    smaller = ModelRootBinding(root.path, tuple(inventory), _canonical_inventory_digest(inventory), None)
    with pytest.raises(CapabilityValidationError, match="absent.*inventory"):
        prepare((client, smaller))
    generation.GenerationFacade.workflow.assert_not_called()


def test_opaque_target_model_alias_is_rejected(producer):
    client, _root = producer
    client.object_info.return_value["VAELoader"]["input"]["required"]["vae_name"] = [[r"FLUX1\ae.safetensors"]]
    with pytest.raises(CapabilityValidationError, match="exact relative"):
        prepare(producer)


def test_preparation_errors_do_not_try_another_catalog_candidate(producer, monkeypatch):
    from astrid.core.generation.backends.vibecomfy import VibeComfyBackend

    bind = Mock(side_effect=ValueError("installed producer binding failed"))
    monkeypatch.setattr(VibeComfyBackend, "prepare_catalog_workflow", bind)
    with pytest.raises(ValueError, match="binding failed"):
        prepare(producer)
    assert bind.call_count == 1
    assert bind.call_args.args[0].id == "z-image"
    generation.GenerationFacade.workflow.assert_not_called()


def test_missing_target_schema_fails_closed(producer, monkeypatch):
    from astrid.core.generation.backends.vibecomfy import VibeComfyBackend

    monkeypatch.setattr(VibeComfyBackend, "prepare_catalog_workflow", lambda *a, **k: (None, None, None))
    with pytest.raises(CapabilityValidationError, match="resolved target model authority"):
        prepare(producer)
