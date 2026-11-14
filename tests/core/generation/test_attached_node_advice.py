"""On an attached ComfyUI, no readiness, preflight or engine message suggests align --apply.

Missing core nodes say to update ComfyUI; missing custom nodes name their pack (and repository
when the lock declares one) and say the person installs and restarts it. Managed messages keep align.
"""
from __future__ import annotations

import json

import pytest
from vibecomfy.node_packs import CustomNodePack

from astrid.core.generation import preflight
from astrid.core.generation import asset_gaps
from astrid.core.generation.asset_gaps import asset_readiness
from astrid.core.generation.backends.comfy_target import (
    ComfyTarget, ComfyTargetClient, _capture_model_resolution,
)
from astrid.core.generation.preflight import GenerationReadinessError, select_generation_target
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.packs.vibecomfy import production_engine
from tests.core.generation.backends.test_comfy_model_resolution import target_info
from tests.core.generation.test_missing_model_attached import FOLDERS, _fake_server
from tests.packs.vibecomfy.test_local_server_run import _workflow, comfy  # noqa: F401 - fixture and workflow

URL = "http://127.0.0.1:8188"
CUSTOM = CustomNodePack(
    name="ComfyUI-Fake", repo="https://github.com/example/ComfyUI-Fake.git",
    classes=frozenset({"AstridFakeNode"}),
)


@pytest.fixture
def known_packs(monkeypatch):
    """Only the fake pack is locked; every other class is a core or unknown node."""
    def resolve(class_types, *, lockfile_path=None):
        return [CUSTOM] if set(class_types) & CUSTOM.classes else []

    monkeypatch.setattr("vibecomfy.node_packs.resolve_node_packs", resolve)


def _assert_no_align(readiness):
    text = json.dumps(readiness)
    assert "align" not in text
    assert "argv" not in text
    assert "astrid generate" not in text
    assert readiness["fixes"], "attached readiness must still carry a fix"
    assert all(fix["scope"] == "attached_target" for fix in readiness["fixes"])


def test_attached_core_node_says_update_comfyui(known_packs):
    readiness = asset_gaps.attached_asset_readiness("z-image", nodes=["CLIPLoader"])
    assert readiness["issues"][0]["message"] == (
        "Your ComfyUI does not have CLIPLoader, which is part of ComfyUI itself; update ComfyUI, then retry."
    )
    _assert_no_align(readiness)


def test_attached_custom_node_names_pack_repository_and_manual_install(known_packs):
    readiness = asset_gaps.attached_asset_readiness("z-image", nodes=["AstridFakeNode"])
    message = readiness["issues"][0]["message"]
    assert "AstridFakeNode" in message
    assert "ComfyUI-Fake" in message
    assert "https://github.com/example/ComfyUI-Fake.git" in message
    assert "custom_nodes" in message
    assert "restart ComfyUI yourself" in message
    _assert_no_align(readiness)


def test_attached_missing_model_names_file_without_align(known_packs):
    readiness = asset_gaps.attached_asset_readiness("z-image", models=["x.safetensors"])
    assert readiness["issues"][0]["code"] == "missing_model"
    assert "x.safetensors" in readiness["issues"][0]["message"]
    _assert_no_align(readiness)


def test_attached_missing_node_in_model_resolution_has_no_align(monkeypatch):
    info = target_info("t2i")
    info.pop("VAELoader")
    _fake_server(monkeypatch, info, FOLDERS)
    client = ComfyTargetClient(ComfyTarget(URL, "attached"))
    with pytest.raises(GenerationReadinessError) as caught:
        _capture_model_resolution(client, ModelRegistry.load_default(), "z-image", "t2i")
    readiness = caught.value.readiness
    assert readiness["issues"][0]["code"] == "missing_node_pack"
    assert readiness["issues"][0]["message"].startswith("Your ComfyUI does not have VAELoader")
    _assert_no_align(readiness)


def test_select_generation_target_keeps_attached_node_readiness_without_align(monkeypatch):
    readiness = {
        "entry": {"id": "z-image", "engine": "comfyui"}, "target": None, "status": "blocked",
        "issues": [{"code": "missing_node_pack", "severity": "error",
                    "message": "Your ComfyUI does not have CLIPLoader, which is part of ComfyUI itself; "
                               "update ComfyUI, then retry.",
                    "details": {"node": "CLIPLoader"}}],
        "fixes": [{"scope": "attached_target", "details": {"action": "update ComfyUI, then retry."}}],
    }

    def refuse(*_args, **_kwargs):
        raise GenerationReadinessError(readiness)

    monkeypatch.setattr(preflight, "_select_generation_target", refuse)
    with pytest.raises(GenerationReadinessError) as caught:
        select_generation_target(URL, entry_id="z-image", mode="t2i")
    _assert_no_align(caught.value.readiness)


def test_managed_missing_node_keeps_align_readiness():
    readiness = asset_readiness("z-image", nodes=["CLIPLoader"])
    assert "align --apply" in readiness["issues"][0]["message"]
    assert readiness["fixes"][0]["argv"][:4] == ["astrid", "generate", "align", "--apply"]


def test_attached_live_preflight_missing_node_names_no_align(comfy, tmp_path):
    workflow = _workflow()
    workflow["nodes"][0].update(
        type="AstridDefinitelyMissingNode",
        properties={"Node name for S&R": "AstridDefinitelyMissingNode"},
        widgets_values=[],
    )
    comfy.workflow.write_text(json.dumps(workflow))
    with pytest.raises(production_engine.ProductionEngineError,
                       match=r"missing nodes: AstridDefinitelyMissingNode.*Missing nodes and models") as caught:
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )
    message = str(caught.value)
    assert "align" not in message
    assert "astrid generate" not in message
    assert "update ComfyUI" in message
    assert not comfy.prompts
