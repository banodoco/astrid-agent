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
    snapshots = {}

    def import_file(*, project, path):
        assert project == "demo"
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        imported[digest] = path
        snapshots[digest] = path.read_bytes()
        return SimpleNamespace(ok=True, data={"object_id": digest, "filename": path.name})

    return SimpleNamespace(media=SimpleNamespace(import_file=Mock(side_effect=import_file),
                                                 snapshots=snapshots)), imported


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
        from vibecomfy.registry.ready import repo_ready_template_discovery, resolve_ready_template
        original = resolve_ready_template('image/z_image_turbo', repo_ready_template_discovery()).path
        source = kwargs['source']
        assert runtime.media.snapshots[source['digest']] == original.read_bytes()
        assert source['filename'] == original.name
        assert source['sha256'] == source['digest'].removeprefix('sha256:')
        assert source['bytes'] == len(original.read_bytes())
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
    # 8d96186db added the higher-priority Qwen candidate; each candidate
    # resolves against a fresh target inventory before z-image is selected.
    assert requests == [("GET", "/system_stats"), ("GET", "/object_info"), ("GET", "/object_info")]
    assert runtime.media.import_file.call_count == 3
    assert not imported[seen["python"]["digest"]].exists()


def test_workflow_source_is_ingested_before_conversion_and_survives_cleanup(network, monkeypatch, tmp_path):
    import json
    from astrid.core.generation import entries
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    client, _ = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    original = b' \r\n' + json.dumps(load_bundle(workflow_from_ready('image/z_image_turbo')).materialize_ui(),
                                    indent=3, ensure_ascii=False).encode() + b'\r\n  '
    path = tmp_path / 'submitted.json'
    path.write_bytes(original)
    runtime, _ = storage_client()
    prepare = entries.prepare
    digest = 'sha256:' + hashlib.sha256(original).hexdigest()

    def convert(*args, **kwargs):
        assert runtime.media.snapshots[digest] == original
        result = prepare(*args, **kwargs)
        path.unlink()
        return result

    monkeypatch.setattr(entries, 'prepare', convert)
    admitted = InvocationResult('vibecomfy.run', 'executor', 'command', True,
                               kernel_task_id='task-admitted', raw_result={'task': {'task_id': 'task-admitted'}})
    calls = []
    monkeypatch.setattr(GenerationFacade, 'workflow', lambda self, **kwargs: calls.append(kwargs) or admitted)
    result = GenerationFacade()._generate_on_target(
        registry=ModelRegistry([]), model=None, mode='t2i', modality='image',
        target=client.target.url, execution=None, inputs={'prompt': 'new prompt'}, dry_run=False,
        workflow_entry={'id': 'workflow', 'kind': 'image', 'modes': ['t2i'], 'bundle': str(path)},
        client=runtime, project='demo', wait=False)
    assert result is admitted  # Admission remains separate from terminal readback.
    assert not path.exists()
    assert calls[0]['wait'] is False
    assert calls[0]['source'] == {'digest': digest, 'sha256': digest[7:],
                                   'bytes': len(original), 'filename': 'submitted.json'}
    assert runtime.media.snapshots[digest] == original


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
    assert plan["entry"]["model"] == "ready-t2i"
    assert plan["entry"]["approval"]["status"] == "approved"


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
    assert plan["entry"]["model"] == "ready"


def test_attached_dry_run_resolves_and_approves_without_destination_or_admission(network, monkeypatch, tmp_path):
    client, requests = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    monkeypatch.setattr(GenerationFacade, "workflow", Mock(side_effect=AssertionError("dry-run admitted")))
    output = tmp_path / "absent"
    plan = GenerationFacade().image(target=client.target.url, prompt="one boat", out=output, dry_run=True)
    assert plan["entry"]["approval"]["status"] == "approved"
    assert plan["target"]["endpoint"] == client.target.url
    # 8d96186db added the higher-priority Qwen candidate; each candidate
    # resolves against a fresh target inventory before z-image is selected.
    assert requests == [("GET", "/system_stats"), ("GET", "/object_info"), ("GET", "/object_info")]
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
    assert raised.value.code == "engine_not_found"
    assert raised.value.fix.startswith('astrid engines install vibecomfy-h3 ')
    assert raised.value.to_envelope()['fixes'][1]['argv'] == [
        'astrid', 'generate', 'image', '--target', 'http://127.0.0.1:8188']
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
    assert plan["target"]["endpoint"] == client.target.url
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
    ("runpod", "missing_dependency"),
    ("https://example.com:8188", "input_rejected"),
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
    assert raised.value.code == "target_unreachable"
    assert "http://127.0.0.1:8123" in raised.value.fix
    assert raised.value.to_envelope()['issues'][0]['details']['engine_code'] == 'COMFY_TARGET_UNAVAILABLE'
