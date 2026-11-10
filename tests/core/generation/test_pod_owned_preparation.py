"""Pod preparation retains custody through cancellation and drainage."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.core.generation import managed_preparation, target_inventory
from astrid.packs.vibecomfy import local_job, production_engine
from astrid.packs.vibecomfy.actions.run import run as action


def test_pod_cancellation_latches_until_preparation_drains(tmp_path, monkeypatch):
    workflow = tmp_path / 'workflow.py'
    workflow.write_text('pass')
    workflow.with_suffix('.vibe.json').write_text('{}')
    workflow.with_name('source.json').write_text('{}')
    events = []

    class Preparation:
        def __init__(self, **kwargs):
            self.cancelled = kwargs['cancelled']
        def prepare(self, path, **kwargs):
            events.append('prepared')
            return {'endpoint': 'http://127.0.0.1:8188'}
        def close(self, *, execution_root, worker_drained):
            assert worker_drained and self.cancelled()
            # A repeated signal during confirmed drainage must not unwind it.
            target_inventory.POD_STOP.get()(15, None)
            events.append('drained')

    monkeypatch.setattr(managed_preparation, 'ManagedEnginePreparation', Preparation)
    monkeypatch.setattr(managed_preparation, 'preparation_scope', lambda report: _scope())
    monkeypatch.setattr(production_engine, 'load_workflow_path', lambda *args: SimpleNamespace(resolved=object()))
    monkeypatch.setattr(production_engine, '_canonical_bundle_value', lambda value: SimpleNamespace(workflow=object()))
    monkeypatch.setattr(production_engine, 'loaded_workflow_execution_identity', lambda value: 'identity')
    def execute(*args, **kwargs):
        stop = target_inventory.POD_STOP.get()
        events.append('execute')
        try:
            stop(15, None)
        except SystemExit:
            stop(2, None)
            events.append('unwound')
            raise
    monkeypatch.setattr(action, '_run_and_settle', execute)
    with pytest.raises(SystemExit) as caught:
        local_job.run_owned_action(workflow, tmp_path / 'out', task_identity='task', attempt_identity='attempt',
                                   align='trust')
    assert caught.value.code == 143
    assert events == ['prepared', 'execute', 'unwound', 'drained']
    assert target_inventory.POD_STOP.get() is None


def _scope():
    from contextlib import nullcontext
    return nullcontext()


def test_pod_refuses_capsule_mutation_before_preparation(tmp_path, monkeypatch):
    workflow = tmp_path / 'workflow.py'
    workflow.write_text('pass')
    workflow.with_suffix('.vibe.json').write_text('{}')
    workflow.with_name('source.json').write_text('{}')

    def load(path, scratch):
        path.write_text('changed')
        return SimpleNamespace(resolved=object())
    monkeypatch.setattr(production_engine, 'load_workflow_path', load)
    monkeypatch.setattr(managed_preparation, 'ManagedEnginePreparation',
                        lambda **kwargs: pytest.fail('must refuse before engine preparation'))
    with pytest.raises(local_job.JobFileError, match='changed during loading'):
        local_job.run_owned_action(workflow, tmp_path / 'out', task_identity='task', attempt_identity='attempt',
                                   align='trust')
    assert target_inventory.POD_STOP.get() is None
