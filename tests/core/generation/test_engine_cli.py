from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from astrid.core import engine_cli
from astrid.core.generation.engine_contracts import CommandFailed, CustodyUnresolved

ROOT = '/TARGET_ONLY/engine'
BASE = ['vibecomfy-h3', '--root', ROOT]


def invoke(args, capsys, **kwargs):
    code = engine_cli.main(args, **kwargs)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_dry_run_is_pure_and_includes_identity(monkeypatch, capsys):
    import builtins
    import socket
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert not any(x in name for x in ('engine_execution', 'engine_platforms', 'runpod', 'credential', 'astrid_runtime'))
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network'))
    monkeypatch.setattr(Path, 'mkdir', lambda *a, **k: pytest.fail('mutation'))
    code, out, err = invoke(['install', *BASE, '--dry-run'], capsys, backend_factory=lambda **kw: pytest.fail('backend'))
    data = json.loads(out)
    assert code == 0 and not err
    assert {step['kind'] for step in data['plan']['steps']} == {'source', 'environment', 'model', 'start', 'health'}
    assert data['plan']['steps'][-1]['depends_on'] == ['start']
    assert data['recipe']['pins']['sha256'] and data['plan']['models_lock_sha256']
    assert data['recipe']['sources'][0]['pin']['revision']
    assert data['recipe']['start']['bind_host'] == '127.0.0.1'


@pytest.mark.parametrize('placement', ['local', 'runpod'])
def test_target_planning(placement, capsys):
    code, out, _ = invoke(['install', *BASE, '--placement', placement, '--dry-run'], capsys)
    assert code == 0 and json.loads(out)['plan']['placement'] == placement


@pytest.mark.parametrize(('args', 'code'), [
    (['install', 'unknown', '--dry-run'], 'unknown_engine'),
    (['install', *BASE, '--platform', 'linux-x86_64-cu124', '--dry-run'], 'unsupported_platform'),
    (['install', 'vibecomfy-h3', '--root', '../bad', '--dry-run'], 'unsafe_path'),
    (['install', *BASE, '--deadline', 'nan', '--dry-run'], 'invalid_recipe'),
    (['install', *BASE], 'unsupported_platform'),
    (['install', *BASE, '--placement', 'runpod'], 'unsupported_placement'),
    (['install', 'vibecomfy-h3', '--platform', 'windows-x86_64-cu130', '--root', 'C:\\engines\\h3', '--placement', 'runpod', '--dry-run'], 'unsupported_platform'),
    (['install', 'vibecomfy-h3', '--platform', 'windows-x86_64-cu130', '--root', 'C:\\engines\\h3'], 'unsupported_platform'),
])
def test_errors(args, code, capsys):
    rc, out, err = invoke(args, capsys)
    assert rc == 1 and not out and json.loads(err)['error']['code'] == code


def test_parser_does_not_echo_untrusted_arguments(capsys):
    with pytest.raises(SystemExit) as exc:
        engine_cli.main(['install', '--secret-token'])
    assert exc.value.code == 2
    assert 'secret-token' not in capsys.readouterr().err


def test_runner_execution_and_missing_status(capsys):
    spec = importlib.util.spec_from_file_location('runner_fixtures', Path(__file__).with_name('test_engine_runner.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    context = fixture.FakeContext()
    seen = []
    def backend(**kwargs):
        seen.append(kwargs)
        context.deadline = kwargs['deadline']
        return context, context.adapter
    rc, out, _ = invoke(['status', *BASE], capsys, backend_factory=backend)
    assert rc == 0 and json.loads(out) is None
    rc, out, _ = invoke(['install', *BASE], capsys, backend_factory=backend)
    result = json.loads(out)
    assert rc == 0 and result['status'] == 'installed' and result['qualified'] is False
    assert seen[-1]['root'] == ROOT and seen[-1]['placement'] == 'local'
    assert any(event[0] == 'download' for event in context.events)


def test_error_is_bounded_and_secret_free(capsys):
    def fail(**kwargs):
        raise CommandFailed('secret' * 10000, step_id='source:comfyui')
    rc, out, err = invoke(['install', *BASE], capsys, backend_factory=fail)
    assert rc == 1 and not out and json.loads(err) == {'error': {'code': 'command_failed', 'step': 'source:comfyui', 'retryable': False, 'recovery_handle': None, 'message': 'Engine operation failed.'}}


def test_recovery_handle_is_preserved(capsys):
    def fail(**kwargs):
        raise CustodyUnresolved("reconcile", recovery_handle="/custody/exact-job-97")
    rc, _, err = invoke(["status", *BASE], capsys, backend_factory=fail)
    assert rc == 1 and json.loads(err)["error"]["recovery_handle"] == "/custody/exact-job-97"


def test_declared_installed_entrypoint_and_module_dispatch(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    script = '''
import importlib, importlib.metadata, json, sys
entry = next(e for e in importlib.metadata.distribution("astrid").entry_points if e.group == "console_scripts" and e.name == "astrid")
assert entry.value == "astrid.console:main"
fn = entry.load()
import astrid.console
assert astrid.console.__file__.startswith(sys.argv[1])
sys.argv = ["astrid", "engines", "install", "vibecomfy-h3", "--root", sys.argv[2], "--dry-run"]
rc = fn()
assert not any(name == "omp" or name.startswith("omp.") or name in ("astrid.omp_agent", "astrid.core.auth") for name in sys.modules)
raise SystemExit(rc)
'''
    root = tmp_path / 'never-created'
    env = dict(os.environ, PYTHONPATH=str(repo), PYTHONDONTWRITEBYTECODE='1')
    for command in ([sys.executable, '-c', script, str(repo), str(root)],
                    [sys.executable, '-m', 'astrid', 'engines', 'install', 'vibecomfy-h3', '--root', str(root), '--dry-run']):
        result = subprocess.run(command, env=env, cwd=tmp_path, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['plan']['engine'] == 'vibecomfy-h3'
        assert not root.exists()
