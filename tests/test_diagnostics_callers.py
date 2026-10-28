"""Cross-worktree producer and diagnostics caller integration."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

AGENT = Path(__file__).resolve().parents[1]
RUNTIME = AGENT.parent / 'astrid-runtime'

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

app_tests = load('review_app_tests', AGENT / 'tests/test_installer_app_update.py')
job_tests = load('review_job_tests', AGENT / 'tests/test_installer_update_job.py')
door = load('review_door', RUNTIME / 'tests/door_helpers.py')

from astrid.installer import update_job, core
from astrid.installer.backend import Backend
from astrid.installer.core import read_json, write_json
from astrid_runtime.paths import _install_record_path
from astrid_runtime.update import read_update_status
from runtime_protocol import platform

def doctor_cli():
    return subprocess.run([sys.executable, '-m', 'astrid_runtime', 'doctor'],
                          capture_output=True, text=True, timeout=30)

@pytest.mark.parametrize('rollback', [False, True])
def test_transaction_producers_to_cli_and_download(tmp_path, monkeypatch, rollback):
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_DATA_HOME', str(home / '.local/share'))
    monkeypatch.setenv('XDG_STATE_HOME', str(home / '.local/state'))
    env = app_tests.installed.__wrapped__(tmp_path, monkeypatch)
    write_json(_install_record_path(home), {'owner': 'haus.astrid.runtime',
               'program_root': str(env.root), 'data_root': str(env.data)})
    env.backend.log = tmp_path / 'actual-installer.log'
    lines = ['installer producer marker', 'Authorization: Bearer auth-secret',
             'Cookie: sid=cookie-secret', '"cookie": "sid=json-cookie-secret"',
             'OPENROUTER_API_KEY=sk-or-v1-openroutersecret', 'token=token-secret',
             'password=password-secret', 'credential=credential-secret', str(home / 'private')]
    Backend.run(env.backend, [sys.executable, '-c', 'print(' + repr('\n'.join(lines)) + ')'])
    assert platform.is_owner_only(env.backend.log)
    if rollback:
        start = env.lifecycle.start
        def fail_candidate(version, *args):
            if version == env.candidate:
                raise ValueError('candidate failed')
            return start(version, *args)
        env.lifecycle.start = fail_candidate
        with pytest.raises(ValueError, match='candidate failed'):
            app_tests.run(env)
    else:
        app_tests.run(env)
    expected = 'rolled_back' if rollback else 'complete'
    assert read_json(env.root / 'update-transaction.json')['step'] == expected
    cli = doctor_cli()
    assert cli.returncode == 1 and 'catalog_missing' in cli.stdout, cli.stderr
    machine = subprocess.run([sys.executable, '-m', 'astrid_runtime', 'doctor', '--json'],
                             capture_output=True, text=True, timeout=30)
    assert machine.returncode == 1
    assert 'catalog_missing' in json.loads(machine.stdout)['issues']
    world = door.start_door_world(tmp_path / 'door')
    try:
        response = world.call('/api/runtime/v1/diagnostics')
        assert response.status == 200
        for report in (cli.stdout, response.json()['report']):
            assert f'Last update result: {expected}' in report
            assert 'installer producer marker' in report
            for secret in ('auth-secret', 'cookie-secret', 'json-cookie-secret',
                           'openroutersecret', 'token-secret', 'password-secret',
                           'credential-secret', str(home)):
                assert secret not in report
        # Actual HTTP admission and log-read boundaries.
        assert world.call('/api/runtime/v1/diagnostics', origin='https://evil.test').status == 403
        assert world.call('/api/runtime/v1/diagnostics', origin=None).status == 403
        env.backend.log.chmod(0o644)
        report = world.call('/api/runtime/v1/diagnostics').json()['report']
        assert 'installer producer marker' not in report
        assert 'log permissions are not owner-only' in report
        original = env.backend.log.with_suffix('.target')
        env.backend.log.rename(original)
        platform.owner_only(original)
        env.backend.log.symlink_to(original)
        report = world.call('/api/runtime/v1/diagnostics').json()['report']
        assert 'installer producer marker' not in report
        # Damage real support authority and read the actual service doctor.
        (world.support / 'catalog.json').write_text('{invalid')
        response = world.call('/api/runtime/v1/diagnostics')
        assert response.status == 200
        assert 'Runtime support state: unhealthy (healthy=False)' in response.json()['report']
        assert 'catalog_invalid' in response.json()['report']
    finally:
        world.stop()
    print(f'{expected}: real transaction + Backend.run -> subprocess doctor/JSON and HTTP; redaction, modes, leaf symlink, Host/Origin and doctor issues passed')

@pytest.mark.parametrize('prior_step', ['complete', 'rolled_back'])
def test_new_failed_job_is_not_hidden_by_prior_direct_journal(tmp_path, monkeypatch, prior_step):
    root, data, old, request = job_tests._fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'home/.local/state'))
    prior_log = platform.log_dir() / 'install.log'
    prior_log.parent.mkdir(parents=True, exist_ok=True)
    Backend.run(SimpleNamespace(log=prior_log), [sys.executable, '-c', "print('previous direct install output')"])
    produce_direct(root, data, old, prior_log, monkeypatch, prior_step)
    prior = read_json(root / 'update-transaction.json')
    assert prior['step'] == prior_step
    bootstrap = Path(request['metadata']['bootstrap_path'])
    bootstrap.write_text('#!/bin/sh\necho "new job failure output"\nexit 23\n')
    request['metadata']['bootstrap_sha256'] = hashlib.sha256(bootstrap.read_bytes()).hexdigest()
    job = update_job.accept(request)
    assert job['prior_transaction_id'] == prior['operation_id']
    # Real worker, input validation, shell subprocess, log capture and terminal
    # result writer. Only the fixture's live owner identity probe is stubbed.
    result = update_job.run_job(root, job['operation_id'])
    expected = 'Failed: Bootstrap exited with status 23 without a verified result.'
    assert result['state'] == 'failed'
    status = read_update_status()
    assert status['outcome'] == 'failed'
    assert status['result'] == expected
    cli = doctor_cli()
    world = door.start_door_world(tmp_path / 'door')
    try:
        response = world.call('/api/runtime/v1/diagnostics')
        assert response.status == 200
        report = response.json()['report']
        http_status = world.call('/api/runtime/v1/update/status')
        assert http_status.status == 200
        assert http_status.json()['result'] == expected
        print('authoritative status:', json.dumps(status))
        print('CLI result:', next(line for line in cli.stdout.splitlines() if line.startswith('Last update result:')))
        print('HTTP result:', next(line for line in report.splitlines() if line.startswith('Last update result:')))
        print('HTTP selected log:', next(line for line in report.splitlines() if line.startswith('Update log:')))
        print('HTTP includes previous output:', 'previous direct install output' in report)
        print('HTTP includes new failure output:', 'new job failure output' in report)
        assert f'Last update result: {expected}' in cli.stdout
        assert f'Last update result: {expected}' in report
        for output in (cli.stdout, report):
            assert 'new job failure output' in output
            assert 'previous direct install output' not in output
    finally:
        world.stop()


def produce_direct(root, data, old, log, monkeypatch, step):
    backend = SimpleNamespace(log=log)
    installer = core.Installer(root, data, backend)
    monkeypatch.setattr(core, 'install_runtime', lambda *_: None)
    monkeypatch.setattr(core, 'install_launcher', lambda *_: None)
    def activate(*_):
        if step == 'rolled_back':
            raise ValueError('candidate failed')
    def run():
        installer._run_update_transaction(old, {}, prepare=lambda: old,
            stop=lambda *_: None, switch_candidate=lambda *_: None,
            switch_old=lambda *_: None, activate=activate, activate_old=lambda *_: None,
            restore_workspace=False, kind='app')
    if step == 'rolled_back':
        with pytest.raises(ValueError, match='candidate failed'):
            run()
    else:
        run()


def test_later_direct_transaction_supersedes_failed_job(tmp_path, monkeypatch):
    root, data, old, request = job_tests._fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'home/.local/state'))
    bootstrap = Path(request['metadata']['bootstrap_path'])
    bootstrap.write_text('#!/bin/sh\necho "older job output"\nexit 23\n')
    request['metadata']['bootstrap_sha256'] = hashlib.sha256(bootstrap.read_bytes()).hexdigest()
    job = update_job.accept(request)
    assert job['prior_transaction_id'] is None
    assert update_job.run_job(root, job['operation_id'])['state'] == 'failed'
    log = platform.log_dir() / 'install.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    Backend.run(SimpleNamespace(log=log), [sys.executable, '-c', "print('later direct output')"])
    produce_direct(root, data, old, log, monkeypatch, 'complete')
    cli = doctor_cli()
    world = door.start_door_world(tmp_path / 'door')
    try:
        response = world.call('/api/runtime/v1/diagnostics')
        assert response.status == 200
        for report in (cli.stdout, response.json()['report']):
            assert 'Last update result: complete' in report
            assert 'later direct output' in report
            assert 'older job output' not in report
    finally:
        world.stop()
