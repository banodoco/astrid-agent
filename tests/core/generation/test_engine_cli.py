from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from dataclasses import dataclass

import pytest

from astrid.core import engine_cli
from astrid.core.generation.engine_contracts import CommandFailed, CustodyUnresolved
from astrid.core.generation.engine_local_endpoint import recorded_engine_root

@dataclass
class InstallResult:
    status: str


ROOT = '/TARGET_ONLY/engine'
BASE = ['vibecomfy-h3', '--root', ROOT]


def invoke(args, capsys, **kwargs):
    code = engine_cli.main(args, **kwargs)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _runner_fixture():
    spec = importlib.util.spec_from_file_location('runner_fixtures', Path(__file__).with_name('test_engine_runner.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture


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


def test_default_factory_constructs_agreed_client(monkeypatch, tmp_path):
    import sys
    import time
    import types
    from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
    captured = {}
    class Client:
        def __init__(self, **kw):
            captured.update(kw)
    module = types.ModuleType('astrid.core.generation.engine_custody')
    module.LinuxEngineServiceClient = Client
    monkeypatch.setitem(sys.modules, module.__name__, module)
    root = str(tmp_path / 'engine')
    context, adapter = engine_cli._default_backend(engine='vibecomfy-h3', platform='linux-x86_64-cu130',
                                                   placement='local', root=root, deadline=time.monotonic() + 10)
    assert isinstance(context, LocalLinuxExecutionContext) and context.adapter is adapter
    assert captured['root'] == root and captured['engine'] == 'vibecomfy-h3'
    assert len(captured['recipe_sha256']) == len(captured['helper_sha256']) == 64
    assert not (tmp_path / 'engine').exists()


def test_successful_local_install_records_its_root(capsys):
    context = _runner_fixture().FakeContext()
    def backend(**kwargs):
        context.deadline = kwargs['deadline']
        return context, context.adapter
    rc, out, _ = invoke(['install', *BASE], capsys, backend_factory=backend)
    assert rc == 0 and json.loads(out)['status'] == 'installed'
    assert recorded_engine_root('vibecomfy-h3') == ROOT


def test_failed_install_records_nothing(capsys):
    def fail(**kwargs):
        raise CommandFailed('secret', step_id='source:comfyui')
    rc, _, _ = invoke(['install', *BASE], capsys, backend_factory=fail)
    assert rc == 1 and recorded_engine_root('vibecomfy-h3') is None


def test_dry_run_and_runpod_install_record_nothing(capsys):
    rc, _, _ = invoke(['install', *BASE, '--dry-run'], capsys, backend_factory=lambda **kw: pytest.fail('backend'))
    assert rc == 0 and recorded_engine_root('vibecomfy-h3') is None
    class RunpodContext(_runner_fixture().FakeContext):
        placement = 'runpod'
    context = RunpodContext()
    def backend(**kwargs):
        context.deadline = kwargs['deadline']
        return context, context.adapter
    rc, out, _ = invoke(['install', *BASE, '--placement', 'runpod'], capsys, backend_factory=backend)
    assert rc == 0 and json.loads(out)['status'] == 'installed'
    assert recorded_engine_root('vibecomfy-h3') is None


def test_fuse_workspace_custody_install_refuses_a_777_directory_before_any_backend(tmp_path, capsys):
    root = tmp_path / 'engine'
    custody = root / '.astrid-engine'
    custody.mkdir(parents=True)
    custody.chmod(0o777)
    code, _, err = invoke(['install', 'vibecomfy-h3', '--root', str(root)], capsys,
                          backend_factory=lambda **kwargs: pytest.fail('backend built for an unsafe custody directory'))
    assert code == 1 and json.loads(err)['error']['code'] == 'custody_not_owner_only'
    assert '/workspace' in err and 'FUSE-unsafe' in err
    assert recorded_engine_root('vibecomfy-h3') is None


def test_fuse_workspace_custody_owner_only_directory_passes_the_preflight(tmp_path, capsys):
    root = tmp_path / 'engine'
    custody = root / '.astrid-engine'
    custody.mkdir(parents=True)
    custody.chmod(0o700)
    reached = []
    def backend(**kwargs):
        reached.append(kwargs['root'])
        raise CommandFailed('stop after preflight', step_id='preflight')
    _, _, err = invoke(['install', 'vibecomfy-h3', '--root', str(root)], capsys, backend_factory=backend)
    assert reached == [str(root)] and 'custody_not_owner_only' not in err



def test_last_explicit_install_drives_implicit_install_status_and_run(tmp_path, monkeypatch, capsys):
    from astrid.core.generation.engine_contracts import HealthEvidence
    from astrid.core.generation.engine_local_endpoint import local_engine_endpoint

    monkeypatch.delenv('ASTRID_ENGINE_ROOT', raising=False)
    roots = [str(tmp_path / 'first'), str(tmp_path / 'second')]
    seen = []
    def backend(**kwargs):
        seen.append(kwargs['root'])
        return object(), object()
    monkeypatch.setattr(engine_cli, 'install_engine', lambda *args, **kwargs: InstallResult(status='installed'))
    monkeypatch.setattr(engine_cli, 'engine_status', lambda *args, **kwargs: None)
    for root in roots:
        rc, _, err = invoke(['install', 'vibecomfy-h3', '--root', root], capsys, backend_factory=backend)
        assert rc == 0, err
    assert recorded_engine_root('vibecomfy-h3') == roots[-1]
    for command in ('status', 'install'):
        rc, _, err = invoke([command, 'vibecomfy-h3'], capsys, backend_factory=backend)
        assert rc == 0, err
    assert seen == roots + [roots[-1], roots[-1]]

    receipt = Path(roots[-1]) / '.astrid-engine' / 'install.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{}')
    run_roots = []
    def status(engine, **kwargs):
        run_roots.append(kwargs['root'])
        return SimpleNamespace(status='installed', health=HealthEvidence('healthy', '2026-10-28T00:00:00Z', 'ab' * 32, True))
    local = local_engine_endpoint(context=object(), status=status)
    assert run_roots == [roots[-1]]
    assert local.endpoint.attestation.install_receipt_path == str(receipt)


def test_implicit_local_root_without_registration_uses_default(monkeypatch, capsys):
    from astrid.core.generation.engine_local_endpoint import EngineNotInstalled, local_engine_endpoint

    monkeypatch.delenv('ASTRID_ENGINE_ROOT', raising=False)
    expected = str(Path.home() / '.astrid-data' / 'engines' / 'vibecomfy-h3')
    seen = []
    def backend(**kwargs):
        seen.append(kwargs['root'])
        return object(), object()
    monkeypatch.setattr(engine_cli, 'install_engine', lambda *args, **kwargs: InstallResult(status='failed'))
    monkeypatch.setattr(engine_cli, 'engine_status', lambda *args, **kwargs: None)
    for command in ('status', 'install'):
        rc, _, err = invoke([command, 'vibecomfy-h3'], capsys, backend_factory=backend)
        assert rc == 0, err
    def status(engine, **kwargs):
        seen.append(kwargs['root'])
        return None
    with pytest.raises(EngineNotInstalled):
        local_engine_endpoint(context=object(), status=status)
    assert seen == [expected, expected, expected]


def test_implicit_cli_root_honors_environment_override(monkeypatch, tmp_path, capsys):
    from astrid.core.generation.engine_local_endpoint import record_engine_root

    record_engine_root('vibecomfy-h3', str(tmp_path / 'registered'))
    override = str(tmp_path / 'override')
    monkeypatch.setenv('ASTRID_ENGINE_ROOT', override)
    rc, out, err = invoke(['install', 'vibecomfy-h3', '--dry-run'], capsys)
    assert rc == 0, err
    assert json.loads(out)['plan']['root'] == override
