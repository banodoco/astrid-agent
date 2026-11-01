"""Catalog target routes retain workflow admission and server ownership."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import hashlib
from unittest.mock import Mock

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.generation import GENERATION_RESULT_KEY, preflight
from astrid.core.generation.backends.base import GenerationResult
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.results import InvocationResult
from tests.core.generation.backends.test_comfy_model_resolution import install_target, target_info
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, network, observation_configuration,
)


def terminal_result(root):
    return InvocationResult(
        "vibecomfy.run", "executor", "command", True,
        raw_result={"payload": {GENERATION_RESULT_KEY: GenerationResult(run_dir=root).to_dict()}},
    )


def storage_client():
    imported = {}

    def import_file(*, project, path):
        assert project == "demo"
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        imported[digest] = path
        return SimpleNamespace(ok=True, data={"object_id": digest, "filename": path.name})

    return SimpleNamespace(media=SimpleNamespace(import_file=Mock(side_effect=import_file))), imported


def attached_without_managed(monkeypatch):
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", Mock(side_effect=AssertionError("managed installation probed")))


def test_attached_default_binds_live_models_and_admits_one_canonical_workflow(network, monkeypatch, tmp_path):
    client, requests = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    seen = {}
    runtime, imported = storage_client()

    def invoke_workflow(self, **kwargs):
        from vibecomfy.security.provenance import Provenance
        from vibecomfy.workflow_bundle import load_bundle

        seen.update(kwargs)
        bundle = load_bundle(imported[kwargs["python"]["digest"]], trust=Provenance.USER_CONFIRMED)
        bundle.require_canonical_authority("test generation admission")
        api = bundle.workflow.compile("api")
        by_class = {node["class_type"]: node["inputs"] for node in api.values()}
        assert by_class["VAELoader"]["vae_name"] == r"FLUX1\ae.safetensors"
        sampler = by_class["KSampler"]
        assert api[sampler["positive"][0]]["inputs"]["text"] == "a silver paper boat"
        assert sampler["seed"] == 42
        assert imported[kwargs["companion"]["digest"]].is_file()
        assert imported[kwargs["source"]["digest"]].is_file()
        return terminal_result(tmp_path)

    workflow = Mock(side_effect=invoke_workflow)
    monkeypatch.setattr(GenerationFacade, "workflow", lambda self, **kwargs: workflow(self, **kwargs))
    monkeypatch.setattr(GenerationFacade, "_invoke", Mock(side_effect=AssertionError("cloud action selected")))
    result = GenerationFacade().image(
        target=client.target.url, client=runtime, project="demo", prompt="a silver paper boat", seed=42,
    )
    assert result.ok is True
    assert workflow.call_count == 1
    assert seen["comfy_url"] == client.target.url
    assert seen["generation_intent"]["modality"] == "image"
    assert seen["generation_intent"]["metadata"]["model"] == "z-image"
    assert requests == [("GET", "/system_stats"), ("GET", "/object_info")]
    assert runtime.media.import_file.call_count == 3
    assert not imported[seen["python"]["digest"]].exists()


@pytest.mark.parametrize("first_candidate", ["wrong-mode", "missing-template"])
def test_default_selects_mode_compatible_model_before_target_resolution(network, monkeypatch, first_candidate):
    import astrid.sdk as sdk

    client, _ = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    entry = ModelRegistry.load_default().get("z-image")
    if first_candidate == "wrong-mode":
        unavailable = replace(entry, id="wrong-mode", modes={"i2i": entry.modes["i2i"]})
    else:
        mode = entry.modes["t2i"]
        spec = replace(mode.backends["local"], template="image/absent-test-template")
        unavailable = replace(entry, id="missing-template", modes={
            "t2i": replace(mode, backends={"local": spec}),
        })
    registry = ModelRegistry([
        unavailable, replace(entry, id="ready-t2i", modes={"t2i": entry.modes["t2i"]}),
    ])
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: registry)
    plan = GenerationFacade().image(target=client.target.url, prompt="one boat", dry_run=True)
    assert plan["model"] == "ready-t2i"
    assert plan["approval"]["status"] == "approved"


def test_default_skips_missing_target_model_and_uses_ready_candidate(network, monkeypatch):
    import astrid.sdk as sdk

    client, _ = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    entry = ModelRegistry.load_default().get("z-image")
    mode = entry.modes["t2i"]
    spec = mode.backends["local"]
    unavailable = replace(spec, model_roles={**spec.model_roles, "vae": replace(spec.model_roles["vae"], names=("absent.safetensors",))})
    registry = ModelRegistry([
        replace(entry, id="absent", modes={"t2i": replace(mode, backends={"local": unavailable})}),
        replace(entry, id="ready", modes={"t2i": mode}),
    ])
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: registry)
    plan = GenerationFacade().image(target=client.target.url, prompt="one boat", dry_run=True)
    assert plan["model"] == "ready"


def test_attached_dry_run_resolves_and_approves_without_destination_or_admission(network, monkeypatch, tmp_path):
    client, requests = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    monkeypatch.setattr(GenerationFacade, "workflow", Mock(side_effect=AssertionError("dry-run admitted")))
    output = tmp_path / "absent"
    plan = GenerationFacade().image(target=client.target.url, prompt="one boat", out=output, dry_run=True)
    assert plan["approval"]["status"] == "approved"
    assert plan["target"] == client.target.url
    assert requests == [("GET", "/system_stats"), ("GET", "/object_info")]
    assert not output.exists()


def test_auto_probes_attached_before_managed_readiness(monkeypatch):
    events = []

    def unavailable(self, path, **kwargs):
        events.append(path)
        raise OSError("offline")

    monkeypatch.setattr(ComfyTargetClient, "request_json", unavailable)
    monkeypatch.setattr(preflight, "_module_available", lambda name: events.append(name) or True)
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", lambda: events.append("managed") or False)
    with pytest.raises(AstridError) as raised:
        GenerationFacade().image(target="auto", prompt="one boat", project="demo")
    assert raised.value.code == "COMFY_TARGET_NOT_READY"
    assert "--target http://127.0.0.1:8188" in raised.value.fix
    assert events == ["/system_stats", "vibecomfy", "managed"]


def test_auto_attached_does_not_require_managed_runtime(network, monkeypatch):
    client, requests = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    original = ComfyTargetClient.__init__

    def default_to_fake(self, target, **kwargs):
        if target.url == "http://127.0.0.1:8188":
            target = ComfyTarget(client.target.url, target.kind, target.facts)
        original(self, target, **kwargs)

    monkeypatch.setattr(ComfyTargetClient, "__init__", default_to_fake)
    plan = GenerationFacade().image(target="auto", prompt="one boat", dry_run=True)
    assert plan["target"] == client.target.url
    assert requests[0] == ("GET", "/system_stats")


def test_managed_retains_install_readiness_and_workflow_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "_module_available", lambda name: True)
    readiness = Mock(return_value=True)
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", readiness)
    monkeypatch.setattr(ComfyTargetClient, "request_json", Mock(side_effect=AssertionError("managed probed attached")))
    admitted = Mock(return_value=terminal_result(tmp_path))
    runtime, _ = storage_client()
    monkeypatch.setattr(GenerationFacade, "workflow", admitted)
    assert GenerationFacade().image(target="managed", client=runtime, project="demo", prompt="one boat").ok
    readiness.assert_called_once_with()
    assert admitted.call_args.kwargs["comfy_url"] is None
    assert admitted.call_args.kwargs["generation_intent"]["modality"] == "image"


@pytest.mark.parametrize("target,code", [
    ("runpod", "COMFY_TARGET_UNSUPPORTED"),
    ("https://example.com:8188", "COMFY_TARGET_INVALID"),
])
def test_unsupported_target_is_coded_before_admission(monkeypatch, target, code):
    monkeypatch.setattr(GenerationFacade, "workflow", Mock(side_effect=AssertionError("invalid target admitted")))
    with pytest.raises(AstridError) as raised:
        GenerationFacade().image(target=target, prompt="one boat")
    assert raised.value.code == code
    assert "--target" in raised.value.fix


def test_explicit_unready_target_names_working_recovery(monkeypatch):
    monkeypatch.setattr(ComfyTargetClient, "request_json", Mock(side_effect=OSError("offline")))
    with pytest.raises(AstridError) as raised:
        GenerationFacade().image(target="http://127.0.0.1:8123", prompt="one boat")
    assert raised.value.code == "COMFY_TARGET_UNAVAILABLE"
    assert "http://127.0.0.1:8123" in raised.value.fix
    assert "--target managed" in raised.value.fix
