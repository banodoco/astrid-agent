"""Whisper is required by transcription preparation, not image runs or engine health."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_recipe import load_recipe
from astrid.core.generation.engine_runner import _object_info_sha256


def _comfy_engine():
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parents[3]
            / "astrid/packs/vibecomfy/extensions/generation.engine/comfyui_engine.py")
    spec = importlib.util.spec_from_file_location("whisper_scope_comfy_engine", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module.ComfyUIEngine()


def test_real_image_preparation_requirements_do_not_need_whisper():
    from astrid.core.generation import entries
    from vibecomfy.registry.ready import workflow_from_ready

    workflow = workflow_from_ready("image/z_image_turbo")
    workflow.set_input("prompt", "a lighthouse on a quiet coast")
    requirements = _comfy_engine().workflow_requirements([workflow])
    image_nodes = {node.class_type for node in workflow.runtime_nodes().values()}

    assert requirements["engine"] == "comfyui"
    assert requirements["models"]
    assert "Apply Whisper" not in image_nodes
    assert all("Whisper" not in row["repo"] for row in requirements["extra"]["nodes"])
    # Exercise the production live target preflight with exactly the image
    # workflow's available nodes: no transcription-only class is installed.
    client = SimpleNamespace(
        object_info=lambda: {name: {} for name in image_nodes},
        target=SimpleNamespace(kind="attached", facts={}),
        http_origin="http://127.0.0.1:8188",
    )
    assert entries.target_schema({"id": "image/z_image_turbo"}, workflow, client) is not None


def test_real_transcription_preflight_refuses_when_whisper_node_is_absent():
    from astrid.core.generation import entries
    from astrid.core.generation.preflight import GenerationReadinessError
    from astrid.packs.generation.workflows.transcription.workflow import build

    workflow = build()
    client = SimpleNamespace(object_info=lambda: {
        "LoadAudio": {}, "VibeComfySaveTranscript": {},
    })
    with pytest.raises(GenerationReadinessError, match="Apply Whisper") as caught:
        entries.target_schema({"id": "audio/transcribe"}, workflow, client)

    assert caught.value.readiness["issues"][0]["code"] == "missing_node_pack"
    assert "install it on the requested ComfyUI target" in caught.value.readiness["issues"][0]["message"]


def test_prepared_shared_engine_health_accepts_image_nodes_without_whisper(monkeypatch):
    from astrid.core.generation import engine_endpoint
    from astrid.core.generation import engine_transport

    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    objects = {name: {} for name in recipe.health.required_nodes if name != "Apply Whisper"}
    endpoint = SimpleNamespace(attestation=SimpleNamespace(
        object_info_sha256=_object_info_sha256(objects), pod_id="pod-test",
    ))

    class Transport:
        def __init__(self, *_args, **_kwargs):
            pass

        def object_info(self, *, deadline):
            assert deadline > 0
            return objects

        def close(self):
            pass

    monkeypatch.setattr(engine_transport, "EngineEndpointTransport", Transport)
    engine_endpoint._probe_prepared_endpoint(
        endpoint, recipe, agent_port=1, deadline=10**12,
    )
