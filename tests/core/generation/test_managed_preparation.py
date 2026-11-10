"""Host-origin preparation refuses drift and retains uncertain native custody."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import CommandResult, CustodyUnresolved, IntegrityError
from astrid.core.generation.managed_preparation import (
    ManagedEnginePreparation, PREPARATION_ENV, SCHEMA, digest, read_preparation,
)


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    source = tmp_path / 'workflow.json'
    source.write_text('{}')
    manifest = {'engine': 'comfyui', 'base': None, 'models': [], 'extra': {'nodes': []},
                'models_root': str(tmp_path / 'models')}
    body = {'schema': SCHEMA, 'task_id': 'task', 'attempt_id': 'attempt', 'execution_identity': 'execution',
        'inputs': {'workflow': hashlib.sha256(source.read_bytes()).hexdigest()},
        'manifest': manifest, 'manifest_sha256': digest(manifest),
        'installation': {'root': str(tmp_path / 'install'), 'receipt_sha256': 'a' * 64,
                         'recipe_sha256': 'b' * 64, 'helper_sha256': 'c' * 64},
        'endpoint': 'http://127.0.0.1:8188', 'output_dir': str(tmp_path / 'output'), 'placement': 'managed',
        'mode': 'check', 'alignment': {'mode': 'check', 'missing': [], 'fetched': [], 'verified': True, 'status': 'ready'},
        'health': {'healthy': True, 'object_info_sha256': 'd' * 64, 'required_nodes_present': True}}
    monkeypatch.delenv('ASTRID_ALIGN', raising=False)
    return body, source


@pytest.mark.parametrize('change', ['task', 'mode', 'verified', 'manifest-extra', 'partial-inputs', 'relative-models', 'relative-output', 'source-drift'])
def test_preparation_refuses_other_attempt_or_changed_inputs(preparation, monkeypatch, change):
    body, source = preparation
    if change == 'task': body['task_id'] = 'another-task'
    if change == 'mode': body['mode'] = 'trust'
    if change == 'verified': body['alignment']['verified'] = False
    if change == 'manifest-extra':
        body['manifest']['lockfile'] = '/ambient/lock'
        body['manifest_sha256'] = digest(body['manifest'])
    if change == 'partial-inputs': body['inputs'] = {'python': 'a' * 64}
    if change == 'relative-models':
        body['manifest']['models_root'] = 'models'
        body['manifest_sha256'] = digest(body['manifest'])
    if change == 'relative-output': body['output_dir'] = 'output'
    if change == 'source-drift': source.write_text('{"changed":true}')
    monkeypatch.setenv(PREPARATION_ENV, json.dumps(body))
    with pytest.raises(ValueError):
        read_preparation(task='task', attempt='attempt', endpoint=body['endpoint'],
                         execution_identity='execution', inputs={'workflow': source})


def test_preparation_accepts_only_exact_host_attempt_bytes(preparation, monkeypatch):
    body, source = preparation
    monkeypatch.setenv(PREPARATION_ENV, json.dumps(body))
    assert read_preparation(task='task', attempt='attempt', endpoint=body['endpoint'],
                            execution_identity='execution', inputs={'workflow': source}) == body


def test_uncertain_preparation_keeps_lock_and_canonical_staging(tmp_path):
    from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path
    originals = {name: tmp_path / name for name in ('python', 'companion', 'source')}
    for path in originals.values(): path.write_text(path.name)
    staged = None
    with pytest.raises(CustodyUnresolved):
        with staged_workflow_path(workflow=None, **originals, scratch=tmp_path / 'staged',
                                  preserve_on_error=(CustodyUnresolved,)) as (workflow, _):
            staged = workflow
            raise CustodyUnresolved('owned command still may read this workflow')
    assert staged is not None and staged.is_file()
    assert staged.with_name('workflow.vibe.json').is_file()
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.uncertain = True
    prep.lock = SimpleNamespace(__exit__=lambda *args: pytest.fail('uncertain lock released'))
    with pytest.raises(CustodyUnresolved): prep.close()


def test_alignment_failure_keeps_native_stderr_out_of_public_error(tmp_path, caplog):
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.engine_platforms import platform_adapter
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.check = lambda: None
    prep.root = str(tmp_path)
    prep.task, prep.attempt = 'task', 'attempt'
    prep.recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    prep.adapter = platform_adapter(prep.recipe.platform)
    prep.deadline = time.monotonic() + 10
    secret = 'PRIVATE_STDERR_SENTINEL'
    prep.context = SimpleNamespace(run=lambda request: CommandResult(9, '', secret, False, 0.1, None, False, False))
    with pytest.raises(IntegrityError) as error:
        prep._command(['doctor', '/workflow.py'])
    assert secret not in str(error.value) and secret not in caplog.text
    assert 'host log' in str(error.value)


def test_configured_models_root_is_read_only_and_refuses_unsafe_receipt(tmp_path):
    from astrid.core.generation.managed_preparation import configured_models_root
    root = tmp_path / 'absent'
    assert configured_models_root(root=str(root)) == str(root / 'models')
    assert not root.exists()
    receipt = root / '.astrid-engine/install.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"invalid":true}')
    with pytest.raises((IntegrityError, ValueError)):
        configured_models_root(root=str(root))


@pytest.mark.parametrize('drained', [False, True])
def test_preparation_retains_lock_when_worker_or_prompt_drainage_is_unconfirmed(tmp_path, monkeypatch, drained):
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.uncertain = False
    prep.report = {'endpoint': 'http://127.0.0.1:8188'}
    released = []
    prep.lock = SimpleNamespace(__exit__=lambda *args: released.append(True))
    def cleanup(*args, **kwargs):
        assert kwargs['confirm'] is True
        raise TimeoutError('owned prompt remains running')
    monkeypatch.setattr('astrid.core.generation.backends.vibecomfy_local_server.cleanup_prompts', cleanup)
    with pytest.raises((CustodyUnresolved, TimeoutError)):
        prep.close(execution_root=tmp_path, worker_drained=drained)
    assert prep.uncertain and prep.lock is not None and not released


def test_prompt_cleanup_confirms_owned_absence_and_never_cancels_foreign(tmp_path, monkeypatch):
    from astrid.core.generation.backends.vibecomfy_local_server import cleanup_prompts
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    path = tmp_path / '.vibecomfy-runtime/out/runs/owned/attempt.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'queue_acceptance': {'status': 'accepted', 'prompt_id': 'owned'}}))
    calls, observations = [], iter([['owned', 'foreign'], ['foreign']])
    class Client:
        def __init__(self, *args): pass
        def witness_prompt(self, value): calls.append(('witness', value))
        def cancel_prompt(self, value): calls.append(('cancel', value))
        def request_json(self, path):
            return {'queue_running': [[0, value] for value in next(observations)], 'queue_pending': []}
    monkeypatch.setattr('astrid.core.generation.backends.vibecomfy_local_server.ComfyTargetClient', Client)
    cleanup_prompts('http://127.0.0.1:8188', tmp_path, confirm=True)
    assert calls == [('witness', 'owned'), ('cancel', 'owned')]


def test_prompt_cleanup_timeout_and_unknown_acceptance_refuse_release(tmp_path, monkeypatch):
    from astrid.core.generation.backends.vibecomfy_local_server import cleanup_prompts
    path = tmp_path / '.vibecomfy-runtime/out/runs/owned/attempt.json'
    path.parent.mkdir(parents=True)
    class Client:
        def __init__(self, *args): pass
        def witness_prompt(self, value): pass
        def cancel_prompt(self, value): pass
        def request_json(self, path): return {'queue_running': [[0, 'owned']], 'queue_pending': []}
    monkeypatch.setattr('astrid.core.generation.backends.vibecomfy_local_server.ComfyTargetClient', Client)
    path.write_text(json.dumps({'queue_acceptance': {'status': 'accepted', 'prompt_id': 'owned'}}))
    with pytest.raises(TimeoutError, match='not drained'):
        cleanup_prompts('http://127.0.0.1:8188', tmp_path, confirm=True, timeout=0)
    path.write_text(json.dumps({'queue_acceptance': {'status': 'not_attempted', 'prompt_id': None},
                               'terminal': {'phase': 'prepared'}}))
    with pytest.raises(ValueError, match='prepared prompt outcome remains unconfirmed'):
        cleanup_prompts('http://127.0.0.1:8188', tmp_path, confirm=True, timeout=0)
    path.write_text(json.dumps({'queue_acceptance': {'status': 'unknown', 'prompt_id': None}}))
    with pytest.raises(ValueError, match='acceptance remains unconfirmed'):
        cleanup_prompts('http://127.0.0.1:8188', tmp_path, confirm=True, timeout=0)
