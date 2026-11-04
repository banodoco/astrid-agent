"""Target roles cross the real canonical compiler without granting byte custody."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web

from astrid.core.generation.backends.comfy_target import (
    ComfyModelMissing, ComfyTarget, ComfyTargetClient, _main, resolve_comfy_models,
)
from astrid.core.generation.backends.vibecomfy import (
    CheckoutServerAdapter, VibeComfyBackend, _bind_target_model_roles,
)
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry, ModelRole, ModeSpec, validate_registry
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, observation_configuration, network,
)

EXACT_VAE = r"FLUX1\ae.safetensors"
RUNTIME = "c3d4e5f6-a7b8-49cd-8e01-23456789abcd"


def target_info(mode="t2i", *, vaes=(EXACT_VAE,), unets=("z_image_turbo_bf16.safetensors",)):
    from vibecomfy.porting.object_info import get_class
    from vibecomfy.registry.ready import workflow_from_ready

    spec = ModelRegistry.load_default().get("z-image").modes[mode].backends["local"]
    workflow = workflow_from_ready(spec.template)
    info = {}
    for node in workflow.runtime_nodes().values():
        entry = get_class(node.class_type)
        assert entry is not None
        info[node.class_type] = {
            "input": copy.deepcopy(entry["inputs"]),
            "input_order": copy.deepcopy(entry["input_order"]),
            "output": [item["type"] for item in entry["outputs"]],
            "output_name": [item["name"] for item in entry["outputs"]],
            "output_is_list": [item["is_list"] for item in entry["outputs"]],
        }
    for cls, field, names in (
        ("UNETLoader", "unet_name", unets),
        ("CLIPLoader", "clip_name", ("qwen_3_4b.safetensors",)),
        ("VAELoader", "vae_name", vaes),
    ):
        info[cls]["input"]["required"][field] = [list(names)]
    return info


def install_target(network, info):
    requests = []

    async def handler(request):
        requests.append((request.method, request.path))
        if request.method == "GET" and request.path == "/object_info":
            return web.json_response(info)
        if request.method == "GET" and request.path == "/system_stats":
            return web.json_response({"system": {"comfyui_version": "0.26.0"}})
        raise AssertionError(f"unexpected target operation: {request.method} {request.path}")

    network["handler"] = handler
    url = f"http://127.0.0.1:{network['port']}"
    return ComfyTargetClient(ComfyTarget(url, "attached")), requests


def with_roles(entry, model_id, roles, **spec_changes):
    mode = entry.modes["t2i"]
    spec = replace(mode.backends["local"], model_roles=roles, **spec_changes)
    return replace(entry, id=model_id, modes={"t2i": replace(mode, backends={"local": spec})})


@pytest.mark.parametrize("mode", ["t2i", "i2i"])
def test_turbo_uses_canonical_approval_and_exact_target_vae(network, monkeypatch, tmp_path, mode):
    client, requests = install_target(network, target_info(mode, unets=("z_image_bf16.safetensors", "z_image_turbo_bf16.safetensors")))
    entry = ModelRegistry.load_default().get("z-image")
    adapter = CheckoutServerAdapter(client.target.url)
    monkeypatch.setattr(adapter, "warm_session", Mock())
    adapter._engine._prepared_runtime_instance_id = RUNTIME
    captured = {}

    def run(workflow, **kwargs):
        from vibecomfy.workflow_bundle import ApprovedProjectionRecord, WorkflowBundle
        approved, bundle = workflow
        assert isinstance(approved, ApprovedProjectionRecord)
        assert isinstance(bundle, WorkflowBundle)
        bundle.require_canonical_authority("acceptance test")
        captured.update(approved.to_dict()["api_projection"])
        return SimpleNamespace(outputs=[])

    monkeypatch.setattr(adapter._engine, "run", run)
    monkeypatch.setattr(adapter, "_collect_outputs", lambda result, out: [])
    params = {"prompt": "a blue lighthouse", "seed": 42, "size": "512x384"}
    if mode == "i2i":
        params["image_ref"] = "astrid/task-1/input.png"
        params["strength"] = 0.4
    adapter.generate(entry, mode, params, tmp_path, model_bytes_digest="sha256:" + "a" * 64, runtime_instance_id=RUNTIME)
    by_class = {node["class_type"]: node["inputs"] for node in captured.values()}
    assert by_class["UNETLoader"]["unet_name"] == "z_image_turbo_bf16.safetensors"
    assert by_class["CLIPLoader"]["clip_name"] == "qwen_3_4b.safetensors"
    assert by_class["VAELoader"]["vae_name"].encode() == EXACT_VAE.encode()
    sampler = by_class["KSampler"]
    assert captured[sampler["positive"][0]]["inputs"]["text"] == params["prompt"]
    assert (sampler["seed"], sampler["steps"], sampler["cfg"]) == (42, 8, 1.0)
    assert (sampler["sampler_name"], sampler["scheduler"]) == ("res_multistep", "simple")
    assert by_class["ModelSamplingAuraFlow"]["shift"] == 3
    if mode == "i2i":
        assert by_class["LoadImage"]["image"] == params["image_ref"]
        assert sampler["denoise"] == 0.4
    assert requests.count(("GET", "/object_info")) == 1
    assert all(method == "GET" for method, _ in requests)


def test_resolution_roundtrip_is_exact_and_does_not_claim_model_bytes(network):
    client, requests = install_target(network, target_info())
    plan = resolve_comfy_models(client, ModelRegistry.load_default(), "z-image", "t2i")
    assert json.loads(json.dumps(plan))["roles"]["vae"] == EXACT_VAE
    assert plan["model_bytes"] == "unverified_target_inventory"
    assert plan["roles"]["diffusion"] == "z_image_turbo_bf16.safetensors"
    assert requests == [("GET", "/object_info")]


@pytest.mark.parametrize("unets", [("z_image_bf16.safetensors",), ("z_image_turbo_bf16-copy.safetensors",)])
def test_base_and_similar_names_are_not_turbo(network, unets):
    client, _ = install_target(network, target_info(unets=unets))
    with pytest.raises(ComfyModelMissing) as raised:
        resolve_comfy_models(client, ModelRegistry.load_default(), "z-image", "t2i")
    assert raised.value.code == "COMFY_MODEL_MISSING"
    assert raised.value.failures == {"diffusion": []}
    assert raised.value.alternatives == []


def test_backslash_name_is_not_repaired(network):
    client, _ = install_target(network, target_info(vaes=("FLUX1/ae.safetensors",)))
    with pytest.raises(ComfyModelMissing) as raised:
        resolve_comfy_models(client, ModelRegistry.load_default(), "z-image", "t2i")
    assert raised.value.failures == {"vae": []}


def test_ambiguous_required_role_fails_closed(network):
    client, _ = install_target(network, target_info(vaes=("ae.safetensors", EXACT_VAE)))
    with pytest.raises(ComfyModelMissing) as raised:
        resolve_comfy_models(client, ModelRegistry.load_default(), "z-image", "t2i")
    assert raised.value.code == "COMFY_MODEL_MISSING"
    assert raised.value.failures == {"vae": ["ae.safetensors", EXACT_VAE]}


@pytest.mark.parametrize("failure", ["missing", "ambiguous"])
def test_alternatives_are_declared_role_matches_that_compile_canonically(network, failure):
    entry = ModelRegistry.load_default().get("z-image")
    spec = entry.modes["t2i"].backends["local"]
    missing = ({**spec.model_roles, "diffusion": replace(spec.model_roles["diffusion"], names=("wanted.safetensors",))} if failure == "missing" else {**spec.model_roles, "vae": replace(spec.model_roles["vae"], names=("ae.safetensors", EXACT_VAE))})
    requested = with_roles(entry, "wanted", missing)
    good = with_roles(entry, "works", spec.model_roles)
    bad_pin = with_roles(entry, "bad-pin", spec.model_roles, template_hash="sha256:" + "0" * 64)
    bad_binding = with_roles(entry, "bad-binding", {**spec.model_roles, "vae": replace(spec.model_roles["vae"], binding="prompt")})
    undeclared = with_roles(entry, "undeclared", {})
    incomplete = with_roles(entry, "incomplete", {**spec.model_roles, "vae": replace(spec.model_roles["vae"], names=("missing.safetensors",))})
    client, requests = install_target(network, target_info(vaes=("ae.safetensors", EXACT_VAE) if failure == "ambiguous" else (EXACT_VAE,)))
    if failure == "ambiguous":
        # The requested entry accepts both names; the executable alternative
        # declares only the exact name it will bind.
        spec_roles = {**spec.model_roles, "vae": replace(spec.model_roles["vae"], names=(EXACT_VAE,))}
        good = with_roles(entry, "works", spec_roles)
    with pytest.raises(ComfyModelMissing) as raised:
        resolve_comfy_models(client, ModelRegistry([requested, good, bad_pin, bad_binding, undeclared, incomplete]), "wanted", "t2i")
    assert raised.value.alternatives == ["works"]
    assert raised.value.as_dict()["code"] == "COMFY_MODEL_MISSING"
    assert requests == [("GET", "/object_info")]


@pytest.mark.parametrize("mode", ["t2i", "i2i"])
def test_checkout_dry_run_compiles_without_submission_or_lifecycle(network, monkeypatch, tmp_path, mode):
    client, requests = install_target(network, target_info(mode))
    adapter = CheckoutServerAdapter(client.target.url)
    monkeypatch.setattr(adapter, "warm_session", Mock(side_effect=AssertionError("dry-run warmed")))
    monkeypatch.setattr(adapter._engine, "run", Mock(side_effect=AssertionError("dry-run submitted")))
    params = {"prompt": "A dry-run lighthouse."}
    if mode == "i2i":
        params["image_ref"] = "astrid/task-1/input.png"
    destination = tmp_path / "dry-run-output"
    plan = adapter.generate(ModelRegistry.load_default().get("z-image"), mode, params, destination, dry_run=True)
    assert plan["approval"]["status"] == "approved"
    assert plan["roles"]["vae"] == EXACT_VAE
    assert requests == [("GET", "/object_info")]
    assert not destination.exists()


def test_cli_dry_run_prints_plan_with_zero_submissions(network, capsys):
    client, requests = install_target(network, target_info())
    assert _main(["z-image", "t2i", "--comfy-url", client.target.url, "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["roles"]["vae"] == EXACT_VAE
    assert requests == [("GET", "/object_info")]


def test_returned_resolution_plan_is_not_an_execution_input(network, tmp_path):
    client, _ = install_target(network, target_info())
    entry = ModelRegistry.load_default().get("z-image")
    plan = resolve_comfy_models(client, ModelRegistry.load_default(), "z-image", "t2i")
    plan["roles"]["diffusion"] = "z_image_bf16.safetensors"
    with pytest.raises(TypeError, match="resolution_plan"):
        VibeComfyBackend(target_client=client).generate(entry, "t2i", {"prompt": "test"}, tmp_path, resolution_plan=plan)


def test_base_adapter_cannot_execute_a_target_approved_graph_on_embedded_runtime(network, monkeypatch, tmp_path):
    import importlib
    runtime = importlib.import_module("vibecomfy.runtime.run")
    client, requests = install_target(network, target_info())
    run = Mock(side_effect=AssertionError("selected target drifted"))
    monkeypatch.setattr(runtime, "run_sync", run)
    with pytest.raises(ValueError, match="selected checkout adapter"):
        VibeComfyBackend(target_client=client).generate(ModelRegistry.load_default().get("z-image"), "t2i", {"prompt": "test"}, tmp_path)
    run.assert_not_called()
    assert requests == [("GET", "/object_info")]


def test_role_binding_preserves_unrelated_authored_requirements():
    from vibecomfy.registry.ready import workflow_from_ready
    entry = ModelRegistry.load_default().get("z-image")
    spec = entry.modes["t2i"].backends["local"]
    workflow = workflow_from_ready(spec.template)
    extra = {"name": "extra.safetensors", "sha256": "a" * 64}
    workflow.requirements.models.append(extra)
    workflow.metadata["model_assets"].append(extra)
    _bind_target_model_roles(workflow, spec, {"diffusion": "z_image_turbo_bf16.safetensors", "text_encoder": "qwen_3_4b.safetensors", "vae": EXACT_VAE})
    assert extra in workflow.requirements.models
    assert extra in workflow.metadata["model_assets"]
    assert EXACT_VAE in workflow.requirements.models
    assert all(item.get("name") != "ae.safetensors" for item in workflow.metadata["model_assets"])


def test_model_role_schema_preserves_exact_names_and_rejects_duplicates():
    raw = {"schema_version": 2, "models": [{"id": "example", "modality": "image", "modes": {"t2i": {"supports": ["prompt"], "requires": ["prompt"], "backends": {"local": {"template": "image/example", "model_roles": {"vae": {"node_class": "VAELoader", "input": "vae_name", "binding": "vae", "names": [EXACT_VAE]}}}}}}}]}
    assert validate_registry(raw)[0].modes["t2i"].backends["local"].model_roles["vae"].names == (EXACT_VAE,)
    raw["models"][0]["modes"]["t2i"]["backends"]["local"]["model_roles"]["vae"]["names"].append(EXACT_VAE)
    with pytest.raises(ValueError, match="distinct"):
        validate_registry(raw)


def test_pod_object_info_uses_existing_attested_transport():
    from astrid.core.generation.engine_transport import EngineEndpointTransport
    transport = Mock(spec=EngineEndpointTransport)
    transport.endpoint = SimpleNamespace(url="https://pod.example/engines/owned")
    transport.object_info.return_value = {"Example": {"input": {}}}
    opener = Mock(side_effect=AssertionError("raw HTTP bypassed attested transport"))
    client = ComfyTargetClient(ComfyTarget(transport.endpoint.url, "pod"), endpoint_transport=transport, opener=opener)
    assert client.object_info() == {"Example": {"input": {}}}
    transport.object_info.assert_called_once()
    assert transport.object_info.call_args.kwargs["deadline"] > 0
    opener.assert_not_called()


def test_engine_forwards_exact_target_provider_to_runtime_reapproval(monkeypatch):
    import importlib
    from vibecomfy.schema.provider import TargetSchemaProvider
    from vibecomfy.workflow_bundle import load_bundle
    from astrid.core.generation.backends.vibecomfy import VibeComfyEngine, _load_pinned_catalog_workflow

    spec = ModelRegistry.load_default().get("z-image").modes["t2i"].backends["local"]
    workflow = _load_pinned_catalog_workflow(spec)
    _bind_target_model_roles(workflow, spec, {"diffusion": "z_image_turbo_bf16.safetensors", "text_encoder": "qwen_3_4b.safetensors", "vae": EXACT_VAE})
    info = target_info()
    refresh = Mock(return_value=info)
    target = TargetSchemaProvider.from_object_info(info, server_url="http://gpu.example.test", model_availability="target", refresh_object_info=refresh)
    bundle = load_bundle(workflow)
    approved = bundle.compile(schema_provider=target)
    called = []

    def run_sync(record, current_bundle, *, server_url, schema_provider):
        assert record is approved and current_bundle is bundle
        assert schema_provider is target
        assert server_url == "http://gpu.example.test"
        from vibecomfy.runtime.session import _prepare_runtime_prompt_with_evidence
        api, skipped = _prepare_runtime_prompt_with_evidence(record, current_bundle, schema_provider=schema_provider)
        assert not skipped
        assert api == record.to_dict()["api_projection"]
        assert schema_provider.refresh()["VAELoader"]["input"]["required"]["vae_name"] == [[EXACT_VAE]]
        called.append(True)
        return SimpleNamespace(outputs=[])

    monkeypatch.setattr(importlib.import_module("vibecomfy.runtime.run"), "run_sync", run_sync)
    engine = VibeComfyEngine("http://gpu.example.test")
    engine._prepared_runtime_instance_id = RUNTIME
    assert engine.run((approved, bundle), runtime_instance_id=RUNTIME, schema_provider=target).outputs == []
    assert called == [True]
    refresh.assert_called_once()


def test_engine_rejects_schema_for_a_different_target():
    from astrid.core.generation.backends.vibecomfy import VibeComfyEngine
    engine = VibeComfyEngine("http://gpu.example.test")
    with pytest.raises(ValueError, match="selected Comfy target"):
        engine.run(object(), runtime_instance_id=RUNTIME, schema_provider=SimpleNamespace(server_url="http://other.example.test"))
    assert not engine._running
