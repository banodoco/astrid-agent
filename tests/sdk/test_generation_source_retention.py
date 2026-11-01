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
