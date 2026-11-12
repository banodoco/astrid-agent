"""Exact review reproducers for context, release priority and discovery."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import failure_bundle as fb
from astrid.packs.runpod import machine as rm
from astrid.packs.runpod import worker_owner as wo
from tests.packs.runpod.test_failure_bundle import _pod


@pytest.mark.parametrize('bad_job', ['missing', 'invalid', 'oversize'])
def test_finding3_missing_context_withholds_text(tmp_path, bad_job):
    _, request = _pod(tmp_path, stderr='violet meadow', engine='violet meadow', bindings={'prompt': 'violet meadow'})
    job = Path(request['run_dir']) / 'job.json'
    if bad_job == 'missing':
        job.unlink()
    elif bad_job == 'invalid':
        job.write_text('{')
    else:
        job.write_text(' ' * (4 * 1024 * 1024 + 1))
    folder, _, facts = fb.collect(request, environ={})
    assert not (Path(folder) / 'error-tail.txt').exists()
    assert not (Path(folder) / 'engine-log-tail.txt').exists()
    assert facts['context_complete'] is False


def test_finding5_controller_secret_reaches_pod_before_shortening(tmp_path):
    _, request = _pod(tmp_path, stderr='.' * 2040 + 'violet meadow password', bindings={})
    request['redaction_context'] = {'complete': True, 'prompts': [], 'secrets': ['violet meadow password']}
    folder, _, _ = fb.collect(request, environ={})
    text = (Path(folder) / 'error-tail.txt').read_text()
    assert 'violet m' not in text
    assert '<secret>' in text


def test_finding3_preparation_supplied_context(tmp_path):
    _, request = _pod(tmp_path, stderr='violet meadow', bindings={})
    request['run_dir'] = None
    request['redaction_context'] = {'complete': True, 'prompts': ['violet meadow'], 'secrets': []}
    folder, _, _ = fb.collect(request, environ={})
    assert 'violet meadow' not in (Path(folder) / 'error-tail.txt').read_text()


def test_finding8_late_release_registration_is_already_aborted():
    owner = SimpleNamespace(lock=threading.RLock(), _observer_lock=threading.Lock(),
                            _failure_observers={}, _release_inflight=True)
    invocation = SimpleNamespace(operation_id='op', owner_epoch='epoch', generation=1)
    owner._read = lambda: {'operation_id': 'op', 'pod_id': 'pod', 'lease': {
        'owner_epoch': 'epoch', 'generation': 1, 'state': 'releasing'}}
    _, event = wo.RunPodLeaseOwner.begin_release_observation(owner, invocation, 'pod')
    assert event.is_set()
    assert not owner._failure_observers


@pytest.mark.parametrize('during', ['fetch', 'redaction', 'deadline', 'cleanup'])
def test_finding9_release_race_cannot_publish_collected(tmp_path, monkeypatch, during):
    event = threading.Event()
    if during == 'cleanup':
        import shutil
        original_remove = shutil.rmtree
        def remove(path, *args, **kwargs):
            if Path(path).name == 'pod':
                event.set()
            return original_remove(path, *args, **kwargs)
        monkeypatch.setattr(shutil, 'rmtree', remove)
    owner = SimpleNamespace(clock=lambda: time.time(), closed=False)
    owner._read = lambda: {}
    ops = SimpleNamespace(token='')
    def execute(*args):
        return 0, fb.MANIFEST_MARK + 'a' * 64, ''
    ops.bounded = lambda *a, **kw: SimpleNamespace(exec=execute)
    machine = rm.RunPodMachine.__new__(rm.RunPodMachine)
    machine._require = lambda: SimpleNamespace(installation=SimpleNamespace(root='/root/astrid/engine'))
    def fetch(*a, **kw):
        if during == 'fetch':
            event.set()
        return {}
    def redact(*a, **kw):
        if during == 'redaction':
            event.set()
        if during == 'deadline':
            monkeypatch.setattr(rm.time, 'monotonic', lambda: 1e20)
        return {'files': [], 'bytes': 0}
    machine._fetch_verified = fetch
    machine._redact_locally = redact
    result = machine._collect_failure(owner=owner, ops=ops, abort_event=event,
        paid_expiry=time.time()+60, limit=None, scratch=tmp_path, name='test', run_dir=None,
        job_dir=None, phase='preparation', exit_code=1, include_install=False, prompts=[], timing=None)
    assert result['state'] == ('timed_out' if during == 'deadline' else 'aborted_by_release')
    assert 'dir' not in result


def test_finding12_discovery_failure_refuses(monkeypatch):
    from astrid.core.extensions.registry import ExtensionRegistry
    from astrid.core.generation import entries
    from astrid.core.pack import source_setup
    def broken(*args, **kwargs):
        raise RuntimeError('discovery unavailable')
    monkeypatch.setattr(ExtensionRegistry, 'load', broken)
    monkeypatch.setattr(entries, 'action_entries', broken)
    assert rm.unshipped_entry_reason('unshipped') is not None
    monkeypatch.setattr(source_setup, 'active_sources', broken)
    with pytest.raises(rm.MachineError):
        rm._active_sources()


def test_finding3_controller_supplies_complete_context_before_prep_export(tmp_path, monkeypatch):
    from tests.packs.runpod import pod_collector
    seen = []
    owner = SimpleNamespace(clock=time.time, closed=False)
    owner._read = lambda: {}
    def execute(command, *args):
        seen.append(pod_collector.decode(command)[1])
        return 0, '', ''
    ops = SimpleNamespace(bounded=lambda *a, **kw: SimpleNamespace(exec=execute))
    machine = rm.RunPodMachine.__new__(rm.RunPodMachine)
    machine._require = lambda: SimpleNamespace(installation=SimpleNamespace(root='/root/astrid/engine'))
    monkeypatch.setattr(rm, '_controller_secrets', lambda ops: ['violet meadow password'])
    machine._collect_failure(owner=owner, ops=ops, abort_event=threading.Event(),
        paid_expiry=time.time()+60, limit=None, scratch=tmp_path, name='prep-test', run_dir=None,
        job_dir=None, phase='preparation', exit_code=1, include_install=False,
        prompts=['violet meadow'], timing=None)
    assert seen[0]['redaction_context'] == {'complete': True, 'prompts': ['violet meadow'],
                                           'secrets': ['violet meadow password']}


@pytest.mark.parametrize('source', ['json', 'secret'])
def test_growing_context_source_is_withheld(monkeypatch, source):
    import io
    if source == 'json':
        monkeypatch.setattr(fb, '_open_regular', lambda path: (io.BytesIO(b' ' * 8 + b'0'), 0))
        with pytest.raises(ValueError):
            fb.read_json('/source', limit=8)
    else:
        monkeypatch.setattr(fb, '_open_regular', lambda path: (io.BytesIO(b'x' * 4097), 0))
        with pytest.raises(ValueError):
            fb.pod_secrets({'secret_files': ['/source']}, environ={})


def test_finding12_shipping_selection_failure_precedes_paid_owner(tmp_path, monkeypatch):
    from astrid.packs.runpod import adapter
    class NoPaidOwner:
        @property
        def lease_owner(self):
            pytest.fail('paid owner reached before shipping verification')
    machine = rm.RunPodMachine.__new__(rm.RunPodMachine)
    machine._require = lambda: NoPaidOwner()
    monkeypatch.setattr(adapter, 'prevalidate_task', lambda *args, **kwargs: None)
    def broken_selection(*args, **kwargs):
        raise RuntimeError('shipping source unavailable')
    monkeypatch.setattr(rm, 'build_selection', broken_selection)
    job = SimpleNamespace(entry=None, policy={}, inputs={}, scratch=tmp_path)
    with pytest.raises(rm.MachineError, match='shipping selection verification failed'):
        machine.claim(job)


def test_controller_defence_pass_removes_known_short_secret(tmp_path, monkeypatch):
    source = tmp_path / 'pod'
    source.mkdir()
    path = source / 'error-tail.txt'
    path.write_text('tulip')
    monkeypatch.setattr(rm, '_controller_secrets', lambda ops: ['tulip'])
    result = rm.RunPodMachine._redact_locally({'error-tail.txt': str(path)}, tmp_path,
                                             prompts=[], ops=SimpleNamespace())
    assert 'tulip' not in (Path(result['dir']) / 'error-tail.txt').read_text()
    assert result['local_counts']['secret'] >= 1
@pytest.mark.parametrize(('inflight', 'state', 'closed', 'record_state'), [
    (True, 'active', False, 'claimed'),
    (False, 'releasing', False, 'cleanup_pending'),
    (False, 'released', False, 'cleanup_complete'),
    (False, 'active', True, 'claimed'),
])
def test_late_job_observation_is_already_aborted(inflight, state, closed, record_state):
    row = {'operation_id': 'op', 'pod_id': 'pod', 'state': record_state,
           'lease': {'owner_epoch': 'epoch', 'generation': 1,
                     'invocation_id': 'invocation', 'state': state}}
    owner = SimpleNamespace(lock=threading.RLock(), _observer_lock=threading.Lock(),
                            _failure_observers={}, _release_inflight=inflight, closed=closed,
                            _read=lambda: row)
    invocation = SimpleNamespace(operation_id='op', owner_epoch='epoch', generation=1,
                                 invocation_id='invocation')
    def preparation_claim(_invocation):
        raise wo.RunPodWorkerOwnerError('preparation requires the exact active invocation lease')
    owner.preparation_claim = preparation_claim
    _, event = wo.RunPodLeaseOwner.begin_failure_observation(owner, invocation, 'pod')
    assert event.is_set()
    assert owner._failure_observers == {}


def test_preparation_with_opaque_input_archive_withholds_text(tmp_path):
    captured = []
    owner = SimpleNamespace(_read=lambda: {'pod_id': 'pod', 'lease': {
        'paid_expires_at': time.time() + 60}},
        begin_release_observation=lambda *args: ('key', threading.Event()),
        end_failure_observation=lambda key: None)
    machine = rm.RunPodMachine.__new__(rm.RunPodMachine)
    def collect(**kwargs):
        captured.append(kwargs)
        return {'state': 'collected'}
    machine._collect_failure = collect
    exc = RuntimeError('preparation failed')
    exc.failure_claim = SimpleNamespace(ops=object(), pod_id='pod')
    job = SimpleNamespace(inputs={'entry_inputs': '/submitted-inputs.tar',
                                  'workflow_inputs': '{"prompt":"violet meadow"}'}, scratch=tmp_path)
    machine._collect_preparation_failure(owner, SimpleNamespace(operation_id='op'), exc, job, None)
    assert captured[0]['prompts'] is None
