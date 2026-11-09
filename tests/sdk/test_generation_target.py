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
from tests.core.generation.availability_fixtures import declare_wan_unqualified
from tests.core.generation.backends.test_comfy_model_resolution import install_target, target_info
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, network, observation_configuration,
)


def terminal_result(root):
    return InvocationResult(
        "vibecomfy.run", "executor", "command", True,
        raw_result={"payload": {GENERATION_RESULT_KEY: GenerationResult(run_dir=root).to_dict()}},
    )


def storage_client(*, store_root=None):
    imported = {}
    snapshots = {}
    retained_paths = {}

    def import_file(*, project, path):
        assert project == "demo"
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        imported[digest] = path
        snapshots[digest] = path.read_bytes()
        if store_root is not None:
            retained = store_root / digest.removeprefix('sha256:') / path.name
            retained.parent.mkdir(parents=True, exist_ok=True)
            retained.write_bytes(snapshots[digest])
            retained_paths[digest] = retained
        return SimpleNamespace(ok=True, data={"object_id": digest, "filename": path.name})

    return SimpleNamespace(media=SimpleNamespace(import_file=Mock(side_effect=import_file),
                                                 snapshots=snapshots, retained_paths=retained_paths)), imported


def attached_without_managed(monkeypatch):
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", Mock(side_effect=AssertionError("managed installation probed")))


def z_image_only_registry(monkeypatch):
    """The target serves z-image; the default owns its failure, so offer no other default."""
    import astrid.sdk as sdk
    registry = ModelRegistry([ModelRegistry.load_default().get('z-image')])
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **kwargs: registry)


def test_default_missing_on_target_owns_its_failure_without_fallback(network, monkeypatch):
    client, requests = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    with pytest.raises(AstridError, match='qwen_image_2.1_turbo_int8_convrot') as caught:
        GenerationFacade().image(placement=client.target.url, prompt='one boat', dry_run=True)
    assert caught.value.code == 'missing_model'
    assert caught.value.to_envelope()['entry']['id'] == 'qwen-image-2.1-turbo'
    assert requests == [('GET', '/system_stats'), ('GET', '/object_info')]


@pytest.mark.parametrize('blocked_ids', [
    ('qwen-image-2.1-turbo', 'last-blocked'), ('z-image', 'last-blocked'),
])
def test_auto_filters_before_probe_and_preparation_preserving_qualified_order(monkeypatch, blocked_ids):
    import astrid.sdk.generation as generation
    from astrid.core.generation import availability, entries
    import astrid.sdk as sdk

    order = ['qwen-image-2.1-turbo', 'z-image', 'qwen-image-2512', 'last-blocked']
    entry = ModelRegistry.load_default().get('z-image')
    registry = ModelRegistry([replace(entry, id=name, modes={'t2i': entry.modes['t2i']})
                              for name in reversed(order)])
    records = {name: {'available': name not in blocked_ids,
        'error': {'code': 'generation_route_unqualified', 'message': f'{name} qualification missing'}}
        for name in order}
    monkeypatch.setattr(availability, 'availability_declaration', lambda: {'models': records})
    checked = []
    original = availability.model_availability
    monkeypatch.setattr(availability, 'model_availability',
                        lambda name, *args: checked.append(name) or original(name, *args))
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **kwargs: registry)
    monkeypatch.setattr(entries, 'user_entries', lambda: [])
    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    events = []
    def probe(*args, **kwargs):
        assert checked == order  # Every declaration checked before the first target probe.
        events.append('probe')
        return None
    monkeypatch.setattr(preflight, 'select_generation_target', probe)
    def prepare(**kwargs):
        assert checked == order
        events.append(kwargs['candidate'].id)
        raise ValueError('fake target missing candidate models')
    monkeypatch.setattr(generation, '_prepare_catalog_target_workflow', prepare)
    with pytest.raises(AstridError) as caught:
        GenerationFacade().image(placement='auto', prompt='boat', dry_run=True)
    assert caught.value.code == 'missing_dependency'
    first = next(name for name in order if name not in blocked_ids)
    assert caught.value.to_envelope()['entry']['id'] == first
    assert 'fake target missing candidate models' in str(caught.value)
    assert events == ['probe', first]


def test_auto_all_unqualified_names_each_route_and_reason_without_probe(monkeypatch):
    import astrid.sdk as sdk
    from astrid.core.generation import availability, entries
    entry = ModelRegistry.load_default().get('z-image')
    registry = ModelRegistry([replace(entry, id=name) for name in ('blocked-a', 'blocked-b')])
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **kwargs: registry)
    monkeypatch.setattr(entries, 'user_entries', lambda: [])
    monkeypatch.setattr(entries, 'ready_entries', lambda: [{
        'id': 'video/wan_fake', 'kind': 'image', 'modes': ['t2i']}])
    monkeypatch.setattr(availability, 'availability_declaration', lambda: {
        'models': {name: {'available': False, 'error': {'code': 'generation_route_unqualified',
                   'message': reason}} for name, reason in [
                       ('blocked-a', 'missing model qualification'), ('blocked-b', 'missing route evidence'),
                       ('templates', 'missing template qualification')]},
        'template_models': {'video/wan': 'templates'}})
    probe = Mock(side_effect=AssertionError('unqualified candidates reached target probe'))
    monkeypatch.setattr(preflight, 'select_generation_target', probe)
    with pytest.raises(AstridError) as caught:
        GenerationFacade().image(placement='auto', prompt='boat', dry_run=True)
    assert caught.value.code == 'generation_route_unqualified'
    for text in ('blocked-a', 'blocked-b', 'video/wan_fake', 'missing model qualification',
                 'missing route evidence', 'missing template qualification'):
        assert text in str(caught.value)
    assert 'qualified' in caught.value.fix
    assert caught.value.to_envelope()['issues'][0]['details']['fix'] == caught.value.fix
    probe.assert_not_called()


def test_qualified_first_candidate_is_probed_and_prepared_first(network, monkeypatch):
    import astrid.sdk as sdk
    import astrid.sdk.generation as generation
    from astrid.core.generation import availability, entries
    client, requests = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    entry = ModelRegistry.load_default().get('z-image')
    registry = ModelRegistry([replace(entry, id='zz-blocked'), entry])
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **kwargs: registry)
    monkeypatch.setattr(entries, 'user_entries', lambda: [])
    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    monkeypatch.setattr(availability, 'availability_declaration', lambda: {'models': {
        'z-image': {'available': True}, 'zz-blocked': {'available': False, 'error': {
            'code': 'generation_route_unqualified', 'message': 'missing qualification'}}}})
    checked = []
    decision = availability.model_availability
    monkeypatch.setattr(availability, 'model_availability',
        lambda name, *args: checked.append(name) or decision(name, *args))
    request = ComfyTargetClient.request_json
    def probe(self, *args, **kwargs):
        assert checked[:2] == ['z-image', 'zz-blocked']
        return request(self, *args, **kwargs)
    monkeypatch.setattr(ComfyTargetClient, 'request_json', probe)
    prepared = []
    prepare = generation._prepare_catalog_target_workflow
    def record_preparation(**kwargs):
        prepared.append(kwargs['candidate'].id)
        return prepare(**kwargs)
    monkeypatch.setattr(generation, '_prepare_catalog_target_workflow', record_preparation)
    plan = GenerationFacade().image(placement=client.target.url, prompt='boat', dry_run=True)
    assert plan['entry']['model'] == 'z-image'
    assert prepared == ['z-image']
    assert requests == [('GET', '/system_stats'), ('GET', '/object_info')]


@pytest.mark.parametrize('target', ['auto', 'managed', 'http://127.0.0.1:8123'])
def test_explicit_unqualified_catalog_is_refused_without_skipping_or_probe(monkeypatch, target):
    declare_wan_unqualified(monkeypatch)
    probe = Mock(side_effect=AssertionError('explicit unqualified route probed'))
    monkeypatch.setattr(preflight, 'select_generation_target', probe)
    with pytest.raises(AstridError) as caught:
        GenerationFacade().video(model='wan-2.2', mode='i2v', placement=target,
            prompt='boat', image_ref='start.png', dry_run=True)
    assert caught.value.code == 'generation_route_unqualified'
    assert 'wan-2.2' in str(caught.value) and 'not been qualified' in str(caught.value)
    assert 'qualified' in caught.value.fix
    probe.assert_not_called()


def test_explicit_unqualified_entry_uses_coded_refusal(monkeypatch):
    import astrid.sdk as sdk
    from astrid.core.generation import availability, entries
    entry = replace(ModelRegistry.load_default().get('z-image'), id='fake-unqualified')
    registry = ModelRegistry([entry])
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **kwargs: registry)
    monkeypatch.setattr(entries, 'user_entries', lambda: [])
    monkeypatch.setattr(entries, 'action_entries', lambda: [])
    monkeypatch.setattr(availability, 'availability_declaration', lambda: {'models': {
        entry.id: {'available': False, 'error': {'code': 'generation_route_unqualified',
                   'message': 'synthetic entry qualification missing'}}}})
    probe = Mock(side_effect=AssertionError('unqualified entry probed'))
    monkeypatch.setattr(preflight, 'select_generation_target', probe)
    with pytest.raises(AstridError) as caught:
        GenerationFacade().image(entry=entry.id, placement='auto', prompt='boat', dry_run=True)
    assert caught.value.code == 'generation_route_unqualified'
    assert entry.id in str(caught.value) and 'synthetic entry qualification missing' in str(caught.value)
    assert caught.value.fix
    probe.assert_not_called()


@pytest.mark.parametrize('inputs,mode,match', [
    ({'prompt': 'boat'}, 'i2v', 'image_ref'),
    ({'prompt': 'boat', 'image_ref': 'start.png'}, 'flf', 'Available backends: cloud'),
])
def test_explicit_unqualified_keeps_input_validation_before_probe(monkeypatch, inputs, mode, match):
    from astrid.sdk.exceptions import CapabilityValidationError, CapabilityMissingInputError
    probe = Mock(side_effect=AssertionError('invalid request probed'))
    monkeypatch.setattr(preflight, 'select_generation_target', probe)
    with pytest.raises((CapabilityValidationError, CapabilityMissingInputError), match=match):
        GenerationFacade().video(model='wan-2.2', mode=mode, placement='auto', dry_run=True, **inputs)
    probe.assert_not_called()


def test_explicit_bundled_template_is_not_filtered_or_dispatched(monkeypatch):
    from astrid.core.generation import entries
    declare_wan_unqualified(monkeypatch)
    manifest = {'id': 'video/wan_fake', 'kind': 'video', 'modes': ['i2v'], 'source': 'ready-template'}
    monkeypatch.setattr(entries, 'find_entry', lambda name: manifest)
    probe = Mock(side_effect=AssertionError('unqualified template probed'))
    monkeypatch.setattr(preflight, 'select_generation_target', probe)
    with pytest.raises(AstridError) as caught:
        GenerationFacade().video(model=manifest['id'], placement='auto', mode='i2v', dry_run=True)
    assert caught.value.code == 'generation_route_unqualified'
    assert manifest['id'] in str(caught.value) and caught.value.fix
    probe.assert_not_called()


@pytest.mark.parametrize('explicit', [False, True])
def test_user_entry_named_like_bundled_wan_is_not_filtered(monkeypatch, explicit):
    from astrid.core.generation import entries
    entry = {'id': 'video/wan_authored', 'kind': 'video', 'modes': ['i2v'],
             'source': 'ready-template', 'bundle': 'authored.py'}
    monkeypatch.setattr(entries, 'user_entries', lambda: [entry])
    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    monkeypatch.setattr(entries, 'action_entries', lambda: [])
    class Probed(Exception):
        pass
    probe = Mock(side_effect=Probed)
    monkeypatch.setattr(preflight, 'select_generation_target', probe)
    with pytest.raises(Probed):
        GenerationFacade()._generate_on_target(registry=ModelRegistry([]),
            model=entry['id'] if explicit else None, mode='i2v', modality='video',
            placement='auto', execution=None, inputs={}, dry_run=True)
    probe.assert_called_once()


def test_attached_default_binds_live_models_and_admits_one_canonical_workflow(network, monkeypatch, tmp_path):
    import builtins

    original_import = builtins.__import__
    def reject_remote_import(name, *args, **kwargs):
        if name == 'astrid.packs.runpod' or name.startswith('astrid.packs.runpod.'):
            raise AssertionError('local generation imported the remote provider')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', reject_remote_import)
    client, requests = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    z_image_only_registry(monkeypatch)
    seen = {}
    runtime, imported = storage_client(store_root=tmp_path / 'runtime-media')

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
        # A01 cleans the import snapshot before admission. The retained source
        # remains a readable file in Runtime media, as A04's check requires.
        assert runtime.media.retained_paths[kwargs["source"]["digest"]].is_file()
        assert runtime.media.retained_paths[source['digest']].read_bytes() == original.read_bytes()
        assert runtime.media.snapshots[source['digest']] == original.read_bytes()
        assert source['filename'] == original.name
        assert source['sha256'] == source['digest'].removeprefix('sha256:')
        assert source['bytes'] == len(original.read_bytes())
        return terminal_result(tmp_path)

    workflow = Mock(side_effect=invoke_workflow)
    monkeypatch.setattr(GenerationFacade, "workflow", lambda self, **kwargs: workflow(self, **kwargs))
    monkeypatch.setattr(GenerationFacade, "_invoke", Mock(side_effect=AssertionError("cloud action selected")))
    result = GenerationFacade().image(
        placement=client.target.url, client=runtime, project="demo", prompt="a silver paper boat", seed=42,
    )
    assert result.ok is True
    assert workflow.call_count == 1
    assert seen["comfy_url"] == client.target.url
    assert seen["generation_intent"]["modality"] == "image"
    assert seen["generation_intent"]["metadata"]["model"] == "z-image"
    assert requests == [("GET", "/system_stats"), ("GET", "/object_info")]
    assert runtime.media.import_file.call_count == 3
    assert not imported[seen["python"]["digest"]].exists()


def test_workflow_source_is_ingested_before_conversion_and_survives_cleanup(network, monkeypatch, tmp_path):
    import json
    from astrid.core.generation import entries
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    # Raw workflows retain their authored picker values. Unlike catalog role
    # binding, this source-retention case needs that exact VAE on the target.
    client, _ = install_target(network, target_info(vaes=('ae.safetensors',)))
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
        placement=client.target.url, execution=None, inputs={'prompt': 'new prompt'}, dry_run=False,
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
    plan = GenerationFacade().image(placement=client.target.url, prompt="one boat", dry_run=True)
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
    plan = GenerationFacade().image(placement=client.target.url, prompt="one boat", dry_run=True)
    assert plan["entry"]["model"] == "ready"


def test_attached_dry_run_resolves_and_approves_without_destination_or_admission(network, monkeypatch, tmp_path):
    client, requests = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    z_image_only_registry(monkeypatch)
    monkeypatch.setattr(GenerationFacade, "workflow", Mock(side_effect=AssertionError("dry-run admitted")))
    output = tmp_path / "absent"
    plan = GenerationFacade().image(placement=client.target.url, prompt="one boat", out=output, dry_run=True)
    assert plan["entry"]["approval"]["status"] == "approved"
    assert plan["target"]["endpoint"] == client.target.url
    assert requests == [("GET", "/system_stats"), ("GET", "/object_info")]
    assert not output.exists()


def test_auto_probes_attached_before_managed_readiness(monkeypatch):
    import platform

    events = []

    # This fixture exercises the indexed local Linux ComfyUI recovery recipe.
    # Pin the supported host premise so the test behaves the same on Darwin.
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")

    def unavailable(self, path, **kwargs):
        events.append(path)
        raise OSError("offline")

    monkeypatch.setattr(ComfyTargetClient, "request_json", unavailable)
    module_probe = Mock(side_effect=AssertionError("legacy module probe used"))
    runtime_probe = Mock(side_effect=AssertionError("legacy runtime probe used"))
    monkeypatch.setattr(preflight, "_module_available", module_probe)
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", runtime_probe)

    def registered_managed_engine():
        events.append("registered-managed")
        return None

    monkeypatch.setattr(preflight, "_registered_managed_engine", registered_managed_engine)
    workflow_admission = Mock(side_effect=AssertionError("workflow admission used"))
    monkeypatch.setattr(GenerationFacade, "workflow", workflow_admission)
    with pytest.raises(AstridError) as raised:
        GenerationFacade().image(placement="auto", prompt="one boat", project="demo")
    assert raised.value.code == "engine_not_found"
    fixes = raised.value.to_envelope()['fixes']
    import shlex
    assert "--placement http://127.0.0.1:8188" in shlex.join(fixes[1]['argv'])
    assert raised.value.fix == 'astrid engines install comfyui --platform linux-x86_64-cu130 --placement local'
    assert fixes[0]['argv'] == [
        'astrid', 'engines', 'install', 'comfyui', '--platform',
        'linux-x86_64-cu130', '--placement', 'local']
    assert fixes[1]['argv'] == [
        'astrid', 'generate', 'image', '--placement', 'http://127.0.0.1:8188']
    assert events == ["/system_stats", "registered-managed"]
    module_probe.assert_not_called()
    runtime_probe.assert_not_called()
    workflow_admission.assert_not_called()


def test_auto_attached_does_not_require_managed_runtime(network, monkeypatch):
    from astrid.core.generation import targets

    client, requests = install_target(network, target_info())
    attached_without_managed(monkeypatch)
    z_image_only_registry(monkeypatch)
    original = targets.resolve_target

    def default_to_fake(target, **kwargs):
        selection = original(target, **kwargs)
        if selection.record and selection.record.endpoint == "http://127.0.0.1:8188":
            selection = replace(selection, record=replace(selection.record, endpoint=client.target.url))
        return selection

    monkeypatch.setattr(targets, "resolve_target", default_to_fake)
    plan = GenerationFacade().image(placement="auto", prompt="one boat", dry_run=True)
    assert plan["target"]["endpoint"] == client.target.url
    assert requests[0] == ("GET", "/system_stats")


def test_managed_retains_install_readiness_and_workflow_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "_module_available", lambda name: True)
    registered = Mock(return_value={"owner": "astrid", "endpoint": "http://127.0.0.1:8188",
                                   "root": str(tmp_path / "managed-engine")})
    monkeypatch.setattr(preflight, "_registered_managed_engine", registered)
    legacy_probe = Mock(side_effect=AssertionError("managed used legacy runtime probe"))
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", legacy_probe)
    monkeypatch.setattr(ComfyTargetClient, "request_json", Mock(side_effect=AssertionError("managed probed attached")))
    admitted = Mock(return_value=terminal_result(tmp_path))
    runtime, _ = storage_client()
    monkeypatch.setattr(GenerationFacade, "workflow", admitted)
    assert GenerationFacade().image(placement="managed", client=runtime, project="demo", prompt="one boat").ok
    assert registered.call_count >= 1
    assert all(call.args == () and call.kwargs == {} for call in registered.call_args_list)
    legacy_probe.assert_not_called()
    assert admitted.call_args.kwargs["comfy_url"] is None
    assert admitted.call_args.kwargs["generation_intent"]["modality"] == "image"


@pytest.mark.parametrize("target,code", [
    ("https://example.com:8188", "input_rejected"),
])
def test_unsupported_target_is_coded_before_admission(monkeypatch, target, code):
    monkeypatch.setattr(GenerationFacade, "workflow", Mock(side_effect=AssertionError("invalid target admitted")))
    with pytest.raises(AstridError) as raised:
        GenerationFacade().image(placement=target, prompt="one boat")
    assert raised.value.code == code
    assert "--placement" in raised.value.fix


def test_runpod_requires_fresh_paid_authority_before_admission(monkeypatch):
    import astrid.sdk as sdk
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **kwargs: ModelRegistry([]))
    monkeypatch.setattr(GenerationFacade, 'workflow', Mock(side_effect=AssertionError('unconsented RunPod admission')))
    with pytest.raises(AstridError) as raised:
        GenerationFacade().image(placement='runpod', prompt='one boat')
    envelope = raised.value.to_envelope()
    assert envelope['status'] == 'blocked' and envelope['task_id'] is None
    assert envelope['issues'][0]['code'] == 'input_rejected'
    assert envelope['issues'][0]['details']['field'] == 'consent'


def test_explicit_unready_target_names_working_recovery(monkeypatch):
    monkeypatch.setattr(ComfyTargetClient, "request_json", Mock(side_effect=OSError("offline")))
    with pytest.raises(AstridError) as raised:
        GenerationFacade().image(placement="http://127.0.0.1:8123", prompt="one boat")
    assert raised.value.code == "target_unreachable"
    assert "http://127.0.0.1:8123" in raised.value.fix
    assert raised.value.to_envelope()['issues'][0]['details']['engine_code'] == 'COMFY_TARGET_UNAVAILABLE'

def _installed_audio_entry(tmp_path, monkeypatch, mode):
    """Install a canonical test entry and real, isolated target preferences."""
    import json
    from astrid.core.generation import targets
    from astrid.packs.runpod import target_selection
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    folder = tmp_path / 'entries' / '00-audio-test'
    folder.mkdir(parents=True)
    monkeypatch.setenv('VIBECOMFY_ENTRIES_DIR', str(folder.parent))
    monkeypatch.setattr(targets, 'config_path', lambda: tmp_path / 'config.toml')
    target_selection.save_runpod_preferences({'provider_account_ref': 'account-a', 'gpu_types': ['gpu-a'],
        'image': 'worker@sha256:' + 'a' * 64})
    workflow = VibeWorkflow('00-audio-test', WorkflowSource('00-audio-test'))
    workflow.add_node('SaveAudio', _id='1', uid='sink', filename_prefix='audio')
    workflow.register_input('filename_prefix', '1', 'filename_prefix', 'audio', type='STRING')
    (folder / 'source.json').write_text(json.dumps({'nodes': [
        {'id': 1, 'type': 'SaveAudio', 'inputs': [], 'widgets_values': ['audio']}], 'links': []}))
    emit_bundle(workflow, folder / 'workflow.py', {'operation': 'authored'})
    (folder / 'entry.json').write_text(json.dumps({'id': '00-audio-test', 'kind': 'audio',
        'modes': [mode], 'bundle': 'workflow.py', 'binds': {}, 'defaults': {}, 'needs': []}))


@pytest.mark.parametrize('mode', ['music', 'tts'])
@pytest.mark.parametrize('explicit', [False, True])
def test_audio_runpod_workflow_admits_existing_receipt_and_policy(mode, explicit, tmp_path, monkeypatch):
    """Exercise the audio composition up to the fake Runtime and storage edges."""
    import json
    _installed_audio_entry(tmp_path, monkeypatch, mode)
    client, _ = storage_client()
    calls = []
    def invoke(capability, **kwargs):
        calls.append((capability, kwargs))
        return InvocationResult(capability, 'executor', 'command', True,
                                kernel_task_id='task-a', raw_result={'state': 'queued'})
    client.invoke_result = invoke
    receipt = tmp_path / 'admission.json'
    result = GenerationFacade().audio(mode=mode, placement='runpod', client=client, project='demo',
        **({'entry': '00-audio-test'} if explicit else {}),
        filename_prefix='test', wait=False, recovery_path=receipt, consent=('paid',), max_usd=2,
        deadline='2099-01-01T00:00:00Z', offer_digest='sha256:' + 'b' * 64,
        preparation_seconds=60, execution_seconds=60, idle_seconds=1800)
    assert result.kernel_task_id == 'task-a' and result.raw_result['state'] == 'queued'
    assert len(calls) == 1
    capability, kwargs = calls[0]
    assert capability == 'vibecomfy.run' and kwargs['recovery_path'] == receipt and not kwargs['wait']
    inputs = kwargs['inputs']
    assert json.loads(inputs['placement_policy'])['kind'] == 'runpod'
    assert json.loads(inputs['workflow_inputs'])['filename_prefix'] == 'test'
    assert inputs['generation_intent']['modality'] == 'audio'
    # Without an entry the slot's preferred default owns the route; its declared
    # output binds filename_prefix exactly as the installed entry's input does.
    from tests.helpers.generation_defaults import default_entries
    SLOT_DEFAULTS = default_entries()
    selected = '00-audio-test' if explicit else SLOT_DEFAULTS[('audio', mode)][0]
    assert inputs['generation_intent']['metadata'] == {'model': selected, 'mode': mode}
    assert all(inputs[name]['digest'].startswith('sha256:') for name in ('source', 'python', 'companion'))


@pytest.mark.parametrize('explicit', [False, True])
def test_foley_runpod_sound_effects_admits_with_placement_policy(tmp_path, monkeypatch, explicit):
    """Sound effects is an ordinary slot: resolves, then admits one task with a RunPod target policy."""
    import json
    _installed_audio_entry(tmp_path, monkeypatch, 'sfx')
    client, _ = storage_client()
    calls = []
    def invoke(capability, **kwargs):
        calls.append((capability, kwargs))
        return InvocationResult(capability, 'executor', 'command', True,
                                kernel_task_id='task-a', raw_result={'state': 'queued'})
    client.invoke_result = invoke
    result = GenerationFacade().audio(mode='sfx', placement='runpod', client=client, project='demo',
        **({'entry': '00-audio-test'} if explicit else {}),
        filename_prefix='test', wait=False, consent=('paid',), max_usd=2,
        deadline='2099-01-01T00:00:00Z', offer_digest='sha256:' + 'b' * 64,
        preparation_seconds=60, execution_seconds=60, idle_seconds=1800)
    assert result.kernel_task_id == 'task-a' and result.raw_result['state'] == 'queued'
    assert len(calls) == 1
    capability, kwargs = calls[0]
    assert capability == 'vibecomfy.run' and not kwargs['wait']
    assert json.loads(kwargs['inputs']['placement_policy'])['kind'] == 'runpod'


@pytest.mark.parametrize('mode', ['music', 'tts'])
def test_audio_runpod_admission_persists_real_runtime_receipt(mode, tmp_path, monkeypatch, real_runtime):
    """Keep the real SDK, local Runtime admission and receipt writer in the route."""
    import json
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.sdk import AstridClient
    from astrid.sdk.remote import RemoteAstridClient
    from astrid.sdk.workspace_client import WorkspaceClient
    _installed_audio_entry(tmp_path, monkeypatch, mode)
    daemon = real_runtime()
    host = None
    try:
        transport = WorkspaceClient(daemon.endpoint, daemon.token)
        client = AstridClient(remote=RemoteAstridClient(transport))
        project = client.projects.create(name='Audio receipt', slug='audio-receipt', idempotency_key='project')
        assert project.ok
        project_id = project.data['project_id']
        packs = Path(__file__).resolve().parents[2] / 'astrid' / 'packs'
        host = GenericPackHost(pack_roots=[packs / 'vibecomfy', packs / 'runpod'],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            credential_source={}, attempt_base=tmp_path / 'attempts')
        host.discover()
        host.preflight()
        host.register()
        receipt = tmp_path / 'receipt.json'
        args = dict(entry='00-audio-test', mode=mode, placement='runpod', client=client, project=project_id,
                    filename_prefix='test', wait=False, recovery_path=receipt, consent=('paid',), max_usd=2,
                    deadline='2099-01-01T00:00:00Z', offer_digest='sha256:' + 'b' * 64,
                    preparation_seconds=60, execution_seconds=60, idle_seconds=1800)
        result = GenerationFacade().audio(**args)
        saved = json.loads(receipt.read_text())
        assert result.ok and result.kernel_task_id == saved['locator']['task_id']
        assert saved['metadata']['capability_id'] == 'vibecomfy.run'
        task = client.tasks.show(result.kernel_task_id)
        assert task.ok and task.data['state'] == 'queued'
        assert not task.data.get('execution_binding')
        resumed = GenerationFacade().audio(**args, resume=True)
        assert resumed.ok and resumed.kernel_task_id == result.kernel_task_id
    finally:
        if host is not None:
            host.shutdown()
        daemon.stop()


@pytest.mark.parametrize('wait', [False])
def test_public_local_foley_stages_video_submits_and_delivers_through_runtime(wait, tmp_path, monkeypatch, capsys, real_runtime):
    """Only the Comfy HTTP/GPU edge is synthetic; use real facade/host/Runtime."""
    import copy
    import io
    import json
    import threading
    import wave
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from email.parser import BytesParser
    from email.policy import default
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from astrid import console
    from astrid.core.execution import generic_host as host_module
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.core.generation import cli
    from astrid.sdk.client import AstridClient
    from vibecomfy.porting.object_info import get_class
    from vibecomfy.registry.ready import workflow_from_ready
    import vibecomfy

    video = tmp_path / 'synthetic.mp4'
    video.write_bytes(b'synthetic-video-at-the-Comfy-HTTP-boundary')
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b'\0\0' * 4800)
    output_bytes = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "wav", "-i", "pipe:0", "-f", "flac", "pipe:1"],
        input=audio.getvalue(), check=True, capture_output=True, timeout=15).stdout
    workflow = workflow_from_ready('audio/hunyuan_video_foley')
    schema = {}
    for node in workflow.runtime_nodes().values():
        definition = get_class(node.class_type)
        assert definition is not None
        schema[node.class_type] = {
            'input': copy.deepcopy(definition['inputs']),
            'input_order': copy.deepcopy(definition['input_order']),
            'output': [out['type'] for out in definition['outputs']],
            'output_name': [out['name'] for out in definition['outputs']],
            'output_is_list': [out['is_list'] for out in definition['outputs']],
            'output_node': node.class_type == 'SaveAudio',
        }
    state = SimpleNamespace(schema=schema, prompts=[], uploaded=None, uploaded_ref=None, views=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, value, media_type='application/json'):
            body = json.dumps(value).encode() if media_type == 'application/json' else value
            self.send_response(200)
            self.send_header('Content-Type', media_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/object_info':
                self.send(state.schema)
            elif self.path == '/system_stats':
                self.send({'system': {'comfyui_version': '0.36.0'}, 'devices': []})
            elif self.path == '/queue':
                self.send({'queue_running': [], 'queue_pending': []})
            elif self.path.startswith('/history/'):
                save = next(key for key, node in state.prompts[0].items() if node['class_type'] == 'SaveAudio')
                self.send({'foley-1': {'outputs': {save: {'audio': [
                    {'filename': 'synthetic.flac', 'subfolder': '', 'type': 'output'}]}},
                    'status': {'completed': True, 'status_str': 'success', 'messages': []}}})
            elif self.path.startswith('/view?'):
                state.views += 1
                self.send(output_bytes, 'audio/flac')
            else:
                self.send_error(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if self.path == '/upload/image':
                message = BytesParser(policy=default).parsebytes(
                    ('Content-Type: ' + self.headers['Content-Type'] + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
                parts = {part.get_param('name', header='content-disposition'): part for part in message.iter_parts()}
                state.uploaded = parts['image'].get_payload(decode=True)
                folder = parts['subfolder'].get_content().strip()
                name = parts['image'].get_filename()
                state.uploaded_ref = folder + '/' + name
                self.send({'name': name, 'subfolder': folder, 'type': 'input'})
            elif self.path == '/prompt':
                state.prompts.append(json.loads(body)['prompt'])
                self.send({'prompt_id': 'foley-1', 'number': 1, 'node_errors': {}})
            elif self.path == '/queue' and json.loads(body) == {'delete': ['foley-1']}:
                self.send({})
            else:
                self.send_error(400)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    daemon = real_runtime()
    client = AstridClient.open(endpoint=daemon.endpoint, credential=daemon.credential_path,
        realm_id=daemon.service.realm['id'], actor_id='owner', client_name='foley-cpu-contract',
        client_version='test', protocol_version='workspace.v1')
    project = client.projects.create(slug='foley', name='Foley CPU fixture', idempotency_key='foley-project')
    assert project.ok
    monkeypatch.setattr(host_module, '_dependency_pythonpath',
                        lambda: (str(Path(vibecomfy.__file__).resolve().parents[1]),))
    host = GenericPackHost(pack_roots=[Path(__file__).resolve().parents[2] / 'astrid/packs/vibecomfy'],
        attempt_root=tmp_path / 'attempt', client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token))
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(client))
    original_invoke = client.invoke_result
    invocations = []

    def bounded_invoke(*args, **kwargs):
        invocations.append(kwargs)
        return original_invoke(*args, **{**kwargs, 'timeout_seconds': 45.0, 'poll_seconds': 0.02})

    monkeypatch.setattr(client, 'invoke_result', bounded_invoke)
    try:
        host.discover()
        host.register()
        if not wait:
            import zipfile
            from vibecomfy.workflow_bundle import load_bundle
            from vibecomfy.security.provenance import Provenance
            receipt_path = tmp_path / 'admission.json'
            code = console.main(['generate', 'audio', '--mode', 'sfx', '--entry', 'audio/hunyuan_video_foley',
                '--prompt', 'Copper bell at the exact frame.', '--video-ref', str(video),
                '--placement', f'http://127.0.0.1:{server.server_port}',
                '--project', project.data['project_id'], '--recovery-path', str(receipt_path), '--no-wait', '--json'])
            captured = capsys.readouterr()
            assert code == 0, captured.out + captured.err
            task_id = json.loads(receipt_path.read_text())['locator']['task_id']
            task = client.tasks.show(task_id)
            assert task.ok and task.data['state'] == 'queued'
            assert len(invocations) == 1
            inputs = invocations[0]['inputs']
            assert inputs['generation_intent']['metadata'] == {'model': 'audio/hunyuan_video_foley', 'mode': 'sfx'}
            folder = tmp_path / 'admitted'
            folder.mkdir()
            for field, filename in [('python', 'workflow.py'), ('companion', 'workflow.vibe.json')]:
                (folder / filename).write_bytes(client.media.read_bytes(inputs[field]['digest']))
            bundle = load_bundle(folder / 'workflow.py', trust=Provenance.USER_CONFIRMED)
            bundle.require_canonical_authority('admitted Foley fixture')
            api = bundle.workflow.compile('api')
            assert next(node for node in api.values() if node['class_type'] == 'HunyuanFoleySampler')['inputs']['prompt'] == 'Copper bell at the exact frame.'
            assert str(video) not in json.dumps(api)
            with zipfile.ZipFile(io.BytesIO(client.media.read_bytes(inputs['managed_assets']['digest']))) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                assert len(manifest['assets']) == 1 and manifest['assets'][0]['binding'] == 'video_ref'
                record = manifest['assets'][0]
                assert archive.read(record['member']) == video.read_bytes()
                assert record['sha256'] == hashlib.sha256(video.read_bytes()).hexdigest()
            assert not state.prompts and state.uploaded is None
            return
        with ThreadPoolExecutor(max_workers=1) as worker:
            settled = worker.submit(host.run, poll_seconds=0.02, max_tasks=1)
            try:
                code = console.main(['generate', 'audio', '--mode', 'sfx', '--entry', 'audio/hunyuan_video_foley',
                    '--prompt', 'Copper bell at the exact frame.', '--video-ref', str(video),
                    '--placement', f'http://127.0.0.1:{server.server_port}',
                    '--project', project.data['project_id'], '--out', str(tmp_path / 'delivered'), '--json'])
            finally:
                host.shutdown()
            results = settled.result(timeout=15)
        captured = capsys.readouterr()
        assert code == 0, captured.out + captured.err
        assert len(results) == 1 and results[0].state == 'succeeded'
        assert state.uploaded == video.read_bytes() and len(state.prompts) == 1
        by_class = {node['class_type']: node['inputs'] for node in state.prompts[0].values()}
        assert by_class['LoadVideo']['file'] == state.uploaded_ref
        assert by_class['HunyuanFoleySampler']['prompt'] == 'Copper bell at the exact frame.'
        assert str(video) not in json.dumps(state.prompts)
        receipt = json.loads((tmp_path / 'attempt/result.json').read_text())
        task = client.tasks.show(receipt['task_id'])
        assert task.ok and task.data['state'] == 'succeeded'
        digest = 'sha256:' + hashlib.sha256(output_bytes).hexdigest()
        assert client.media.read_bytes(digest) == output_bytes and state.views > 0
        assert any(path.read_bytes() == output_bytes for path in (tmp_path / 'delivered').rglob('*.flac'))
    finally:
        host.shutdown()
        daemon.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize(('given', 'forwarded'), [({}, 'check'), ({'align': 'trust'}, 'trust')])
def test_workflow_target_forwards_align_without_a_name_error(given, forwarded, monkeypatch, tmp_path):
    """workflow(target=...) takes align like image/video/audio and hands it to the target route."""
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import emit_bundle

    emit_bundle(workflow_from_ready('image/z_image_turbo'), tmp_path / 'workflow.py', {'operation': 'authored'})
    seen = []
    monkeypatch.setattr(GenerationFacade, '_generate_on_target',
                        lambda self, **kwargs: seen.append(kwargs) or 'routed')
    assert GenerationFacade().workflow(workflow=tmp_path / 'workflow.py', placement='http://127.0.0.1:8188',
                                       dry_run=True, **given) == 'routed'
    assert seen[0]['align'] == forwarded


def test_workflow_target_rejects_an_unknown_align(tmp_path):
    from astrid.sdk.generation import GenerationInputError
    with pytest.raises(GenerationInputError, match='align must be one of'):
        GenerationFacade().workflow(workflow=tmp_path / 'workflow.py', placement='http://127.0.0.1:8188',
                                    align='maybe')
