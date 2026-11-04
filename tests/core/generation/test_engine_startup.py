"""Real subprocess fixtures test orchestration, never a real engine installation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from astrid.core.generation import engine_startup as startup
from astrid.core.generation.engine_recipe import load_model_lock, load_recipe
from astrid.core.generation.engine_runner import plan_install

# This deliberately tiny CLI only writes fixture receipts. It installs nothing.
FAKE_CLI = r'''
import json, os, pathlib, sys, time
mode, template, events, *args = sys.argv[1:]
root = pathlib.Path(args[args.index('--root') + 1])
command = args[1]
root.mkdir(parents=True, exist_ok=True)
with open(events, 'a') as stream:
    stream.write(command + '\n')
assert not any(k in os.environ for k in ('ASTRID_POD_TOKEN', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'RUNPOD_API_KEY', 'PYTHONPATH'))
(root / 'install.started').write_text('started')
if mode == 'timeout':
    print('fixture still running', file=sys.stderr, flush=True)
    time.sleep(60)
if mode == 'noisy':
    for _ in range(40):
        sys.stderr.write('fixture-output-' * 1024)
    sys.stderr.flush()
value = json.loads(pathlib.Path(template).read_text())
count_file = root / 'fake.count'
count = int(count_file.read_text()) if count_file.exists() else 0
if command == 'install':
    count_file.write_text(str(count + 1))
    if count:
        for row in value['steps']:
            row['action'] = 'skipped'
        for row in value['models']:
            row['action'] = 'skipped'
    if mode == 'bad-rerun' and count:
        value['steps'][0]['action'] = 'executed'
    if mode == 'failure':
        value['status'] = 'failed'
        value['error_code'] = 'command_failed'
    path = root / '.astrid-engine/install.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')))
    os.replace(temporary, path)
if mode == 'success':
    time.sleep(0.15)
if mode == 'bad-status' and command == 'status':
    value['status'] = 'failed'
    value['error_code'] = 'integrity_failed'
print(json.dumps(value), flush=True)
raise SystemExit(7 if mode == 'failure' else 0)
'''


def _fixture(tmp_path, mode='success'):
    root = tmp_path / 'engine'
    recipe = load_recipe('vibecomfy-h3', 'linux-x86_64-cu130')
    plan = plan_install(recipe, root=str(root), placement='local')
    models = load_model_lock(recipe.models_lock)
    stamp = '2026-10-27T22:00:00+00:00'
    value = {'schema': 'astrid.engine-install.v1', 'receipt_id': 'fake-cli-fixture',
             'engine': plan.engine, 'platform': plan.platform, 'placement': 'local',
             'root': str(root), 'recipe_sha256': plan.recipe_sha256,
             'models_lock_sha256': plan.models_lock_sha256,
             'requirements_locks': {e.name: '1' * 64 for e in recipe.environments},
             'source_commits': {s.name: s.pin.revision for s in recipe.sources},
             'models': [{'model': m.name, 'path': str(root / 'models' / m.destination),
                         'action': 'skipped', 'bytes': m.bytes, 'sha256': m.sha256,
                         'verified': True, 'resumed_from_bytes': 0} for m in models.models],
             'steps': [{'id': s.id, 'kind': s.kind, 'action': 'executed',
                        'validation': 'passed', 'facts': {}} for s in plan.steps],
             'health': {'status': 'healthy', 'checked_at': stamp,
                        'object_info_sha256': '2' * 64, 'required_nodes_present': True},
             'started_at': stamp, 'completed_at': stamp, 'status': 'installed',
             'qualified': False, 'error_code': None}
    template, fake, events = tmp_path / 'receipt.json', tmp_path / 'fake_cli.py', tmp_path / 'events'
    template.write_text(json.dumps(value))
    fake.write_text(FAKE_CLI)
    return {'cli_prefix': (sys.executable, '-I', '-S', str(fake), mode, str(template), str(events)),
            'root': root, 'observations': tmp_path / 'public', 'private': tmp_path / 'private',
            'timeout_seconds': 10, 'step_seconds': 4}, events


def test_installer_starts_before_failed_health_and_finishes_all_three_steps(tmp_path, monkeypatch):
    kwargs, events = _fixture(tmp_path)
    # Poison parent environment: it must not reach the canonical CLI process.
    monkeypatch.setenv('ASTRID_POD_TOKEN', 'fixture-private-token')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'fixture-not-a-real-key')
    monkeypatch.setenv('PYTHONPATH', '/untrusted')
    health = tmp_path / 'unavailable_health.py'
    health.write_text('''
import pathlib, sys, time
root = pathlib.Path(sys.argv[1])
end = time.monotonic() + 5
while not (root / 'install.started').exists():
    if time.monotonic() >= end:
        raise SystemExit(99)
    time.sleep(.01)
(root / 'health.failed').write_text('unavailable')
raise SystemExit(7)
''')
    observer = []
    def start_health():
        observer.append(subprocess.Popen([sys.executable, '-I', '-S', str(health), str(kwargs['root'])]))
    kwargs['on_install_started'] = start_health
    reads, stop = [], threading.Event()
    def reader():
        path = kwargs['observations'] / 'status.json'
        while not stop.is_set():
            if path.exists():
                reads.append(json.loads(path.read_text()))
            stop.wait(.002)
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        result = startup.run_sequence(**kwargs)
        assert observer[0].wait(timeout=5) == 7
    finally:
        stop.set()
        thread.join(timeout=2)
    assert reads and all(isinstance(row, dict) for row in reads)
    assert (kwargs['root'] / 'health.failed').read_text() == 'unavailable'
    assert result['state'] == 'succeeded' and result['qualified'] is False
    assert events.read_text().splitlines() == ['install', 'status', 'install']
    assert [row['stage'] for row in result['steps']] == ['first', 'status', 'rerun']
    receipt = kwargs['root'] / '.astrid-engine/install.json'
    assert result['receipt_path'] == str(receipt)
    assert result['receipt']['sha256'] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert result['receipt']['snapshot_sha256'] == result['receipt']['sha256']
    assert all(s['action'] == 'skipped' for s in json.loads(receipt.read_text())['steps'])
    assert json.loads((kwargs['observations'] / 'status.json').read_text()) == result
    assert (kwargs['observations'] / 'first-install.json').is_file()
    assert (kwargs['observations'] / 'rerun-install.json').is_file()


def test_observer_launch_exception_cannot_gate_install(tmp_path):
    kwargs, events = _fixture(tmp_path)
    def unavailable():
        raise OSError('fixture agent cannot bind')
    result = startup.run_sequence(**kwargs, on_install_started=unavailable)
    assert result['state'] == 'succeeded'
    assert result['steps'][0]['observation_launch_failed'] is True
    assert events.read_text().splitlines() == ['install', 'status', 'install']


def test_failure_writes_status_and_preserves_canonical_receipt_without_retry(tmp_path):
    kwargs, events = _fixture(tmp_path, 'failure')
    result = startup.run_sequence(**kwargs)
    assert result['state'] == 'failed'
    assert events.read_text().splitlines() == ['install']
    receipt = json.loads((kwargs['root'] / '.astrid-engine/install.json').read_text())
    assert receipt['status'] == 'failed'
    assert result['receipt']['path'] == str(kwargs['root'] / '.astrid-engine/install.json')
    assert json.loads((kwargs['observations'] / 'status.json').read_text())['state'] == 'failed'


@pytest.mark.parametrize('mode,expected', [('bad-status', ['install', 'status']),
                                          ('bad-rerun', ['install', 'status', 'install'])])
def test_success_exit_cannot_fake_healthy_status_or_all_skipped_rerun(tmp_path, mode, expected):
    kwargs, events = _fixture(tmp_path, mode)
    result = startup.run_sequence(**kwargs)
    assert result['state'] == 'failed'
    assert events.read_text().splitlines() == expected


def test_hung_cli_is_stopped_with_bounded_failure_and_logs(tmp_path):
    kwargs, events = _fixture(tmp_path, 'timeout')
    kwargs.update(timeout_seconds=2, step_seconds=.3)
    started = time.monotonic()
    result = startup.run_sequence(**kwargs)
    assert time.monotonic() - started < 3
    assert result['state'] == 'failed' and result['steps'][0]['timed_out']
    assert result['steps'][0]['exit_code'] is not None
    assert result['logs']['stderr']['retained_bytes'] > 0
    assert events.read_text().splitlines() == ['install']


def test_cancellation_is_bounded_and_does_not_rerun(tmp_path):
    kwargs, events = _fixture(tmp_path, 'timeout')
    stop = threading.Event()
    timer = threading.Timer(.2, stop.set)
    timer.start()
    try:
        result = startup.run_sequence(**kwargs, stop_event=stop)
    finally:
        timer.join()
    assert result['state'] == 'failed' and result['steps'][0]['cancelled']
    assert events.read_text().splitlines() == ['install']


def test_output_is_drained_with_closed_segments_and_total_cap(tmp_path):
    kwargs, _ = _fixture(tmp_path, 'noisy')
    result = startup.run_sequence(**kwargs, segment_bytes=1024, stream_bytes=4096)
    assert result['state'] == 'succeeded'
    facts = result['logs']['stderr']
    assert facts['retained_bytes'] == 4096 and facts['discarded_bytes'] > 0 and facts['truncated']
    assert len(facts['files']) == 4
    for row in facts['files']:
        path = Path(row['path'])
        assert path.stat().st_size <= 1024
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']
        assert path.with_name(path.name + '.sha256').read_text().strip() == row['sha256']
    assert not list(kwargs['observations'].glob('*.tmp'))


def test_existing_attempt_and_symlink_paths_refused(tmp_path):
    kwargs, _ = _fixture(tmp_path)
    assert startup.run_sequence(**kwargs)['state'] == 'succeeded'
    with pytest.raises(FileExistsError):
        startup.run_sequence(**kwargs)
    other = tmp_path / 'other'
    other.mkdir()
    link = tmp_path / 'linked'
    link.symlink_to(other, target_is_directory=True)
    kwargs['observations'] = link
    with pytest.raises(Exception):
        startup.run_sequence(**kwargs)


def test_shutdown_uncertainty_preserves_existing_recovery_handle(tmp_path):
    from astrid.core.generation.engine_contracts import CustodyUnresolved
    kwargs, _ = _fixture(tmp_path)
    result = startup.run_sequence(**kwargs)
    assert result['state'] == 'succeeded'
    handle = 'a2:root:' + '1' * 64 + ':owner:' + '2' * 32 + ':op:' + '3' * 32
    def failed_stop(*, deadline):
        assert deadline > time.monotonic()
        raise CustodyUnresolved('private fixture diagnostic', recovery_handle=handle)
    with pytest.raises(CustodyUnresolved):
        startup._finish_session(failed_stop, kwargs['observations'])
    raw = (kwargs['observations'] / 'status.json').read_text()
    status = json.loads(raw)
    assert status['state'] == 'failed' and status['session_stop'] == 'unresolved'
    assert status['recovery_handle'] == handle
    assert 'private fixture diagnostic' not in raw
    assert status['receipt'] == result['receipt']


@pytest.mark.parametrize('value', [0, -1, float('inf'), float('nan'), True])
def test_invalid_budget_refused_before_cli(tmp_path, value):
    kwargs, events = _fixture(tmp_path)
    kwargs['timeout_seconds'] = value
    with pytest.raises(ValueError):
        startup.run_sequence(**kwargs)
    assert not events.exists()
