"""Catalog roles resolve exact target choices before any prompt is submitted."""
from __future__ import annotations

import pytest

from astrid.core.generation.backends.comfy_target import ComfyModelMissing, resolve_comfy_models
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry, ModelRole, ModeSpec


def role(node, field, names, binding):
    return ModelRole(node_class=node, input=field, names=tuple(names), binding=binding)


def entry(model_id, roles, *, mode="t2i", supports=("prompt",), requires=("prompt",)):
    return ModelEntry(
        id=model_id, modality="image", modes={mode: ModeSpec(
            supports=supports, requires=requires,
            backends={"local": BackendSpec(template="image/example", model_roles=roles)},
        )},
    )


def object_info(**choices):
    return {node: {"input": {"required": {field: [names]}}}
            for node, (field, names) in choices.items()}


class Target:
    def __init__(self, info):
        self.info = info
        self.endpoint_transport = None
        self.requests = []
        self.submissions = []

    def request_json(self, path, *args, **kwargs):
        self.requests.append(path)
        assert path == "/object_info"
        return self.info

    def submit_prompt(self, payload):
        self.submissions.append(payload)
        raise AssertionError("resolution submitted a prompt")


def test_roles_use_exact_target_names_including_backslashes():
    vae = r"FLUX1\ae.safetensors"
    registry = ModelRegistry([entry("example", {
        "unet": role("UNETLoader", "unet_name", ["turbo.safetensors"], "3"),
        "vae": role("VAELoader", "vae_name", [vae], "2"),
    })])
    client = Target(object_info(
        UNETLoader=("unet_name", ["base.safetensors", "turbo.safetensors"]),
        VAELoader=("vae_name", [vae]),
    ))
    plan = resolve_comfy_models(client, registry, "example", "t2i")
    assert plan["model"] == "example"
    assert plan["mode"] == "t2i"
    assert plan["roles"] == {"unet": "turbo.safetensors", "vae": vae}
    assert plan["roles"]["vae"].encode() == vae.encode()
    assert client.requests == ["/object_info"]
    assert client.submissions == []


def test_missing_role_is_coded_and_alternatives_are_satisfiable():
    need = {"unet": role("UNETLoader", "unet_name", ["wanted.safetensors"], "3")}
    available = {"unet": role("UNETLoader", "unet_name", ["available.safetensors"], "3")}
    incomplete = {**available, "vae": role("VAELoader", "vae_name", ["missing.safetensors"], "2")}
    registry = ModelRegistry([entry("wanted", need), entry("works", available), entry("incomplete", incomplete)])
    client = Target(object_info(UNETLoader=("unet_name", ["available.safetensors"])))
    with pytest.raises(ComfyModelMissing) as raised:
        resolve_comfy_models(client, registry, "wanted", "t2i")
    assert raised.value.code == "COMFY_MODEL_MISSING"
    assert "works" in str(raised.value.alternatives)
    assert "incomplete" not in str(raised.value.alternatives)
    assert client.submissions == []


def test_multiple_exact_declared_choices_fail_closed():
    registry = ModelRegistry([entry("ambiguous", {
        "unet": role("UNETLoader", "unet_name", ["one.safetensors", "two.safetensors"], "3"),
    })])
    client = Target(object_info(UNETLoader=("unet_name", ["one.safetensors", "two.safetensors"])))
    with pytest.raises(ComfyModelMissing) as raised:
        resolve_comfy_models(client, registry, "ambiguous", "t2i")
    assert raised.value.code == "COMFY_MODEL_MISSING"
    assert client.submissions == []


def test_cli_dry_run_prints_plan_and_sends_no_prompt(network, monkeypatch, capsys):
    from aiohttp import web
    from astrid.core.generation.backends.comfy_target import _main

    vae = r"FLUX1\ae.safetensors"
    registry = ModelRegistry([entry("example", {
        "vae": role("VAELoader", "vae_name", [vae], "2"),
    })])
    monkeypatch.setattr(ModelRegistry, "load_default", lambda: registry)
    requests = []

    async def handler(request):
        requests.append((request.method, request.path))
        if request.path == "/object_info":
            return web.json_response(object_info(VAELoader=("vae_name", [vae])))
        raise AssertionError(f"unexpected request: {request.method} {request.path}")

    network["handler"] = handler
    assert _main(["example", "t2i", "--comfy-url", f"http://127.0.0.1:{network['port']}", "--dry-run"]) == 0
    import json
    assert json.loads(capsys.readouterr().out) == {"model": "example", "mode": "t2i", "roles": {"vae": vae}}
    assert requests == [("GET", "/object_info")]


# Reuse G22's real local HTTP server fixture.
from tests.core.generation.backends.test_comfy_target import (  # noqa: E402,F401
    network, engine_configuration, observation_configuration,
)


def test_nearby_names_are_not_repaired_or_treated_as_equivalent():
    vae = r"FLUX1\ae.safetensors"
    registry = ModelRegistry([entry("example", {
        "vae": role("VAELoader", "vae_name", [vae], "2"),
    })])
    client = Target(object_info(VAELoader=("vae_name", ["FLUX1/ae.safetensors", "ae.safetensors"])))
    with pytest.raises(ComfyModelMissing) as raised:
        resolve_comfy_models(client, registry, "example", "t2i")
    assert raised.value.code == "COMFY_MODEL_MISSING"
    assert raised.value.failures == {"vae": []}
    assert client.submissions == []


@pytest.mark.parametrize("mode", ["t2i", "i2i"])
def test_z_image_resolved_workflow_binds_prompt_turbo_and_exact_vae(mode, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from astrid.core.generation.backends.vibecomfy import VibeComfyBackend

    registry = ModelRegistry.load_default()
    model = registry.get("z-image")
    vae = r"FLUX1\ae.safetensors"
    info = object_info(
        UNETLoader=("unet_name", ["z_image_bf16.safetensors", "z_image_turbo_bf16.safetensors"]),
        CLIPLoader=("clip_name", ["qwen_3_4b.safetensors"]),
        VAELoader=("vae_name", [vae]),
    )
    plan = resolve_comfy_models(Target(info), registry, "z-image", mode)
    captured = {}

    def capture(workflow):
        from vibecomfy.workflow_bundle import load_bundle
        captured.update(load_bundle(workflow).compile().api_projection)
        return SimpleNamespace(outputs=[])

    backend = VibeComfyBackend()
    monkeypatch.setattr(backend, "_run_workflow", capture)
    params = {"prompt": "a blue lighthouse", "seed": 42}
    if mode == "i2i":
        params["image_ref"] = "example.png"
    backend.generate(model, mode, params, tmp_path, resolution_plan=plan)
    unet, clip, vae_node, prompt, sampler_id = ("3", "1", "2", "5", "8") if mode == "t2i" else ("2", "3", "4", "6", "10")
    assert captured[unet]["inputs"]["unet_name"] == "z_image_turbo_bf16.safetensors"
    assert captured[clip]["inputs"]["clip_name"] == "qwen_3_4b.safetensors"
    assert captured[vae_node]["inputs"]["vae_name"] == vae
    assert captured[prompt]["inputs"]["text"] == "a blue lighthouse"
    if mode == "i2i":
        assert captured["1"]["inputs"]["image"] == "example.png"
    sampler = captured[sampler_id]["inputs"]
    assert (sampler["seed"], sampler["steps"], sampler["cfg"]) == (42, 9, 1.0)
    assert sampler["sampler_name"] == "euler"
    assert sampler["scheduler"] == "simple"
