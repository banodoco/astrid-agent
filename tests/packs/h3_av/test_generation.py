from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.generation import generation_timing
from astrid.packs.h3_av.shared.stages import _generation_intent


def _locked_model_workflow(**kwargs):
    from astrid.packs.h3_av.shared.generation import build_generation_workflow

    return build_generation_workflow(
        references=["reference.png"], prompt="A scene.", frames=24,
        model="minimax_h3_ref2va_pruned_int8_convrot.safetensors", steps=8, seed=1,
        **kwargs,
    )


def test_generation_assets_use_recipe_lock_revisions():
    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    from astrid.packs.h3_av.shared import generation

    lock = load_recipe_model_lock("comfyui", "linux-x86_64-cu130")
    workflow = _locked_model_workflow(adapter_strength=0.7)
    assets = {f'{row["subdir"]}/{row["name"]}': row for row in workflow.metadata["model_assets"]}
    assert len(lock.models) == 5
    assert set(assets) == {model.destination for model in lock.models} | {
        f"loras/{generation.CHARACTER_SWAP_FILENAME}"
    }
    for model in lock.models:
        assert assets[model.destination]["url"] == (
            f"https://huggingface.co/{model.repository}/resolve/{model.revision}/{model.path}"
        )
    character = assets[f"loras/{generation.CHARACTER_SWAP_FILENAME}"]
    assert character["url"] == (
        f"https://huggingface.co/{generation.CHARACTER_SWAP_REPOSITORY}/resolve/"
        f"{generation.CHARACTER_SWAP_REVISION}/{generation.CHARACTER_SWAP_FILENAME}"
    )
    assert character["hf_revision"] == generation.CHARACTER_SWAP_REVISION
    assert f"loras/{generation.TURBO_FILENAME}" not in {
        f'{row["subdir"]}/{row["name"]}'
        for row in _locked_model_workflow(turbo_enabled=False).metadata["model_assets"]
    }


@pytest.mark.parametrize("invalid", [
    {"revision": None}, {"revision": "main"}, {"repository": None},
    {"path": None}, {"url": "https://huggingface.co/owner/repo/resolve/main/model"},
])
def test_generation_rejects_incomplete_or_unpinned_lock_entry(monkeypatch, invalid):
    from astrid.packs.h3_av.shared import generation

    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    lock = load_recipe_model_lock("comfyui", "linux-x86_64-cu130")
    broken = replace(lock.models[0], **invalid)
    _install_lock(replace(lock, models=(broken, *lock.models[1:])))
    with pytest.raises(ValueError, match="pinned Hugging Face source"):
        _locked_model_workflow()


@pytest.mark.parametrize("index", range(5))
def test_generation_rejects_missing_required_lock_destination(monkeypatch, index):
    from astrid.packs.h3_av.shared import generation

    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    lock = load_recipe_model_lock("comfyui", "linux-x86_64-cu130")
    missing = lock.models[index]
    _install_lock(replace(lock, models=tuple(model for model in lock.models if model is not missing)))
    with pytest.raises(ValueError, match="missing destination") as error:
        _locked_model_workflow()
    assert missing.destination in str(error.value)


def _install_lock(lock):
    """Hand the pack a modified model lock through its engine facts (the autouse fixture restores them)."""
    from astrid.core.generation.engine_facts import comfyui_facts
    from astrid.packs.h3_av.shared import engine_facts
    engine_facts.install(replace(comfyui_facts(), locked_models=lambda: {m.destination: m for m in lock.models}))


def request(count=4, **updates):
    raw = {"version": 1, "operation": "generate", "output": {"duration": 15},
           "content": {"prompt": "A subject crosses the scene in <Picture 1>."},
           "changes": {"video": [], "audio": []},
           "references": [{"asset": f"image-{i}", "purpose": "appearance"} for i in range(count)],
           "overrides": {"steps": 8, "seed": 42}}
    raw.update(updates)
    return normalize_request(raw)




@pytest.mark.parametrize("updates,match", [
    ({"source": {"asset": "video"}}, "source must be omitted"),
    ({"references": []}, "one to nine"),
    ({"references": [{"asset": "a", "purpose": "audio"}]}, "image guidance"),
    ({"references": [{"asset": "a", "purpose": "keyframe"}]}, "timed keyframes"),
    ({"output": {"duration": 15.01}}, "24 fps"),
    ({"output": {"duration": 16}}, "5..362"),
    ({"output": {"duration": float("inf")}}, "finite"),
    ({"references": [{"asset": "a", "purpose": "style"}] * 10}, "nine"),
    ({"changes": {"video": [], "audio": [{"during": [0, 1], "action": "preserve"}]}}, "changes/masks"),
])
def test_unsupported_generation_requests_fail_at_admission(updates, match):
    with pytest.raises(H3RequestError, match=match):
        request(**updates)


def test_generation_timing_covers_original_fifteen_second_request():
    assert generation_timing(15) == {"fps": 24, "requested_frames": 360,
                                   "raw_frames": 362, "trim_tail_frames": 2, "duration": 15}


@pytest.mark.integration
def test_reference_must_decode_as_still_image(tmp_path: Path):
    path = tmp_path / "fake.png"
    path.write_bytes(b"not an image")
    with pytest.raises(CompilationError, match="decodable still image"):
        compile_preparation(prepare_request(request(1), asset_map={"image-0": str(path)}), out_dir=tmp_path / "compiled")






def _runpod_request():
    return {
        "target": {"kind": "runpod", "pod_id": "original-pod",
                   "provider_account_ref": "account-1",
                   "storage": {"network_volume_id": "original-volume"}},
        "remote_activation_required": True,
        "lifecycle": {"mode": "leave_running"},
    }


def _canonical_result(**changes):
    from astrid.sdk.results import InvocationResult
    return InvocationResult(
        capability_id="vibecomfy.run", capability_type="executor", native_kind="executor",
        kernel_task_id="task-1", kernel_run_id="run-1", kernel_attempt_id="attempt-1",
        **{"ok": True, **changes},
    )


@pytest.fixture
def prepared_client(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from astrid.packs.runpod.prepared_task import PreparedWorkerSelection
    final = _canonical_result()
    underlying = SimpleNamespace(invoke_result=Mock(side_effect=[_canonical_result(), final]))

    def show(task_id):
        assert task_id == "task-1"
        return {"task_id": task_id, "run_id": "run-1", "attempt_id": "attempt-1",
                "capability_id": "vibecomfy.run",
                "state": "queued" if underlying.invoke_result.call_count == 1 else "succeeded",
                "execution_binding": {"effective_target": _runpod_request()["target"]}}

    underlying.tasks = SimpleNamespace(show=show)
    owner = SimpleNamespace(ensure_for_task=Mock(), retire_after_task=Mock())
    factory = Mock(return_value=owner)
    selection = PreparedWorkerSelection(
        claim_handle_path=tmp_path / "claim.json", deployment_profile_path=tmp_path / "profile.json",
        staging_inputs_path=tmp_path / "staging.json", journal_dir=tmp_path / "owner-journal",
        target=_runpod_request()["target"], deployment_profile={}, staging_inputs={},
        composition_factory=factory,
    )
    prepared = selection.bind_client(
        underlying, workflow_path=tmp_path / "workflow.py",
        workflow_bundle_path=tmp_path, workflow_inputs={"seed": 0},
    )
    prepared.invoke_result = Mock(wraps=prepared.invoke_result)
    return prepared, underlying, owner, factory, final














