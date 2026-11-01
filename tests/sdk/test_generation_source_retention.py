"""Source ingestion precedes conversion and task admission; no engine is run."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.core.generation import entries, preflight
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.results import InvocationResult


@pytest.fixture
def admission(monkeypatch):
    stored = {}
    paths = []

    def import_file(*, project, path):
        assert project == 'demo'
        data = path.read_bytes()
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        stored[digest] = data
        paths.append(path)
        return SimpleNamespace(ok=True, data={'object_id': digest})

    client = SimpleNamespace(media=SimpleNamespace(import_file=import_file))
    monkeypatch.setattr(preflight, '_module_available', lambda name: True)
    monkeypatch.setattr(preflight, '_comfyui_runtime_available', lambda: True)
    task = InvocationResult('vibecomfy.run', 'executor', 'command', True,
                            kernel_task_id='task-1', raw_result={'task': {'task_id': 'task-1'}})
    invoke = Mock(return_value=task)
    workflow_method = GenerationFacade.workflow
    monkeypatch.setattr(GenerationFacade, 'workflow', invoke)
    return SimpleNamespace(stored=stored, paths=paths, client=client, task=task, invoke=invoke,
                           workflow_method=workflow_method)


def test_public_workflow_imports_before_description_converts_source(monkeypatch, admission, tmp_path):
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle
    data = b' \r\n' + json.dumps(load_bundle(workflow_from_ready(
        'image/z_image_turbo')).materialize_ui(), indent=3).encode() + b'\r\n '
    path = tmp_path / 'submitted.json'
    path.write_bytes(data)
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    resolve = entries.resolve_workflow_inputs

    def describe(*args, **kwargs):
        assert admission.stored[digest] == data
        return resolve(*args, **kwargs)

    monkeypatch.setattr(entries, 'resolve_workflow_inputs', describe)
    result = admission.workflow_method(GenerationFacade(), workflow=path, target='managed',
        inputs={'prompt': 'bound prompt'}, project='demo', client=admission.client, wait=False)
    assert result is admission.task
    assert admission.invoke.call_args.kwargs['source'] == {
        'digest': digest, 'sha256': digest[7:], 'bytes': len(data), 'filename': path.name}
    assert admission.stored[digest] == data


def test_catalog_imports_exact_pinned_source_before_build(monkeypatch, admission):
    from astrid.core.generation.backends import vibecomfy as backend
    from vibecomfy.registry import ready

    registry = ModelRegistry.load_default()
    spec = registry.get('z-image').modes['t2i'].backends['local']
    original = ready.resolve_ready_template(spec.template, ready.repo_ready_template_discovery()).path
    data = original.read_bytes()
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    build = ready.workflow_from_ready

    def require_original(*args, **kwargs):
        assert admission.stored[digest] == data
        return build(*args, **kwargs)

    monkeypatch.setattr(ready, 'workflow_from_ready', require_original)
    # Bypass unrelated entry discovery; exercise the real pinned catalog load.
    monkeypatch.setattr(entries, 'find_entry', lambda name: None)
    result = GenerationFacade()._generate_on_target(
        registry=registry, model='z-image', mode='t2i', modality='image', target='managed',
        execution=None, inputs={'prompt': 'changed prompt', 'seed': 43}, dry_run=False,
        client=admission.client, project='demo', wait=False)
    assert result is admission.task
    supplied = admission.invoke.call_args.kwargs
    assert supplied['source'] == {'digest': digest, 'sha256': digest[7:],
                                  'bytes': len(data), 'filename': original.name}
    assert supplied['wait'] is False
    assert all(not path.exists() for path in admission.paths)
    assert admission.stored[digest] == data


@pytest.mark.parametrize('named_entry', [False, True])
def test_submitted_source_bytes_survive_conversion_and_cleanup(monkeypatch, admission, tmp_path, named_entry):
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import emit_bundle, load_bundle

    workflow = workflow_from_ready('image/z_image_turbo')
    data = b' \r\n' + json.dumps(load_bundle(workflow).materialize_ui(), indent=3).encode() + b'\r\n '
    path = tmp_path / ('source.json' if named_entry else 'submitted.json')
    path.write_bytes(data)
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    bundle = emit_bundle(workflow, tmp_path / 'workflow.py', {'operation': 'authored'}) if named_entry else None
    manifest = {'id': 'mine', 'kind': 'image', 'modes': ['t2i'],
                'bundle': str(bundle.python_path if bundle else path)}
    monkeypatch.setattr(entries, 'find_entry', lambda name: manifest)
    prepare = entries.prepare

    def convert(*args, **kwargs):
        assert admission.stored[digest] == data
        prepared = prepare(*args, **kwargs)
        path.unlink()
        return prepared

    monkeypatch.setattr(entries, 'prepare', convert)
    result = GenerationFacade()._generate_on_target(
        registry=ModelRegistry([]), model='mine' if named_entry else None,
        mode='t2i', modality='image', target='managed', execution=None,
        inputs={'prompt': 'changed prompt'}, dry_run=False,
        workflow_entry=None if named_entry else manifest,
        client=admission.client, project='demo', wait=False)
    assert result is admission.task
    assert not path.exists()
    assert admission.invoke.call_args.kwargs['source'] == {
        'digest': digest, 'sha256': digest[7:], 'bytes': len(data), 'filename': path.name}
    assert admission.stored[digest] == data


@pytest.mark.parametrize('canonical', [False, True])
def test_public_workflow_freezes_execution_before_importer_changes_original(
    monkeypatch, admission, tmp_path, canonical,
):
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle, load_bundle
    from vibecomfy.security.provenance import Provenance

    workflow = VibeWorkflow('a01', WorkflowSource('a01'))
    workflow.add_node('SaveImage', _id='1', uid='save', filename_prefix='original')
    workflow.add_node('CLIPTextEncode', _id='12', uid='prompt', text='original')
    workflow.register_input('prompt', '12', 'text', 'original', type='STRING')
    (tmp_path / 'source.json').write_text(json.dumps({'nodes': [
        {'id': 1, 'type': 'SaveImage', 'inputs': [], 'widgets_values': ['original']},
        {'id': 12, 'type': 'CLIPTextEncode', 'inputs': [], 'widgets_values': ['original']}], 'links': []}))
    bundle = emit_bundle(workflow, tmp_path / 'workflow.py', {'operation': 'authored'})
    path = bundle.python_path
    if not canonical:
        path = tmp_path / 'submitted.json'
        path.write_bytes(b' \r\n' + json.dumps(load_bundle(workflow).materialize_ui()).encode() + b'\r\n ')
    original = path.read_bytes()
    digest = 'sha256:' + hashlib.sha256(original).hexdigest()
    importer = admission.client.media.import_file

    def replace_original(**kwargs):
        result = importer(**kwargs)
        path.write_bytes(b'the user replaced the original file')
        return result

    admission.client.media.import_file = replace_original
    result = admission.workflow_method(GenerationFacade(), workflow=path, target='managed',
        inputs={'prompt': 'requested'}, project='demo', client=admission.client, wait=False)
    assert result is admission.task
    supplied = admission.invoke.call_args.kwargs
    assert admission.stored[digest] == original
    assert supplied['source'] == {'digest': digest, 'sha256': digest[7:],
                                  'bytes': len(original), 'filename': path.name}
    frozen = tmp_path / 'readback'
    frozen.mkdir()
    (frozen / 'workflow.py').write_bytes(admission.stored[supplied['python']['digest']])
    (frozen / 'workflow.vibe.json').write_bytes(admission.stored[supplied['companion']['digest']])
    applied = load_bundle(frozen / 'workflow.py', trust=Provenance.USER_CONFIRMED).workflow
    assert applied.nodes['12'].inputs['text'] == 'requested'
    assert all(not imported.exists() for imported in admission.paths)


def test_stale_bundle_rejects_before_target_or_admission(monkeypatch, tmp_path):
    from astrid.sdk.generation import GenerationInputError
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    workflow = VibeWorkflow('a01', WorkflowSource('a01'))
    workflow.add_node('SaveImage', _id='1', uid='save', filename_prefix='original')
    bundle = emit_bundle(workflow, tmp_path / 'workflow.py', {'operation': 'authored'})
    bundle.python_path.with_suffix('.vibe.json').write_text('{}')
    monkeypatch.setattr(preflight, 'select_generation_target', lambda *a, **k: pytest.fail('target probe'))
    with pytest.raises(GenerationInputError, match='canonical workflow bundle'):
        GenerationFacade().workflow(workflow=bundle.python_path, target='auto', dry_run=True)


@pytest.mark.parametrize('inputs', [[], '', 1])
def test_workflow_inputs_require_an_object_before_source_read(inputs):
    from astrid.sdk.generation import GenerationInputError
    with pytest.raises(GenerationInputError, match='must be an object'):
        GenerationFacade().workflow(workflow='absent.py', target='auto', inputs=inputs)


def test_missing_native_entry_api_returns_readiness_before_target(monkeypatch, tmp_path):
    source = tmp_path / 'submitted.json'
    source.write_bytes(b'{}')
    def unavailable(*args, **kwargs):
        raise ModuleNotFoundError('native workflow entry API is absent', name='vibecomfy.entries')
    monkeypatch.setattr(entries, 'resolve_workflow_inputs', unavailable)
    monkeypatch.setattr(preflight, 'select_generation_target', lambda *a, **k: pytest.fail('target probe'))
    with pytest.raises(preflight.GenerationReadinessError) as raised:
        GenerationFacade().workflow(workflow=source, target='auto', dry_run=True)
    issue = raised.value.to_envelope()['issues'][0]
    assert issue['code'] == 'missing_dependency'
    assert issue['details'] == {'field': 'workflow', 'module': 'vibecomfy.entries'}


@pytest.mark.parametrize('change', ['stale', 'missing'])
def test_saved_entry_keeps_exact_source_when_original_changes(monkeypatch, admission, tmp_path, change):
    from vibecomfy.entries import import_as_entry
    import astrid.sdk as sdk

    source = tmp_path / 'submitted.json'
    data = b' \r\n' + json.dumps({'nodes': [
        {'id': 1, 'type': 'SaveImage', 'inputs': [], 'widgets_values': ['original']},
        {'id': 12, 'type': 'CLIPTextEncode', 'inputs': [], 'widgets_values': ['original']}],
        'links': []}, indent=3).encode() + b'\r\n '
    source.write_bytes(data)
    monkeypatch.setenv('VIBECOMFY_ENTRIES_DIR', str(tmp_path / 'entries'))
    saved = import_as_entry(source, 'a01-saved')
    before = {p: p.read_bytes() for p in (tmp_path / 'entries').rglob('*') if p.is_file()}
    if change == 'missing':
        source.unlink()
    else:
        source.write_bytes(b'the external source has changed')
    monkeypatch.setattr(entries, 'action_entries', lambda: [])
    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **kwargs: ModelRegistry([]))
    result = GenerationFacade().image(entry=saved['id'], target='managed',
        inputs={'prompt': 'requested'}, client=admission.client, project='demo', wait=False)
    assert result is admission.task
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    assert admission.invoke.call_args.kwargs['source']['digest'] == digest
    assert admission.stored[digest] == data
    assert {p: p.read_bytes() for p in (tmp_path / 'entries').rglob('*') if p.is_file()} == before
