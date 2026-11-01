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


@pytest.fixture(autouse=True)
def ample_disk(monkeypatch):
    monkeypatch.setattr(engine_cli, '_disk_anchor', lambda root: ('/', 1, 1, 10**15))


@pytest.mark.parametrize(('free', 'status'), [(10**15, 'ok'), (1, 'blocked')])
def test_install_disk_check_is_read_only_on_refusal(tmp_path, monkeypatch, capsys, free, status):
    root = tmp_path / 'new' / 'engine'
    from astrid.core.generation import engine_runner
    monkeypatch.setattr(engine_cli, '_disk_anchor', engine_runner._disk_anchor)
    monkeypatch.setattr(engine_runner.os, 'fstatvfs', lambda fd: SimpleNamespace(f_bavail=free, f_frsize=1))
    seen = []
    monkeypatch.setattr(engine_cli, 'install_engine', lambda *a, **kw: seen.append('install') or InstallResult('installed'))
    def backend(**kwargs):
        seen.append('backend')
        return object(), object()
    monkeypatch.setattr('astrid.core.generation.engine_local_endpoint.record_engine_root', lambda *a: None)
    rc, out, err = invoke(['install', 'vibecomfy-h3', '--root', str(root)], capsys, backend_factory=backend)
    if status == 'blocked':
        error = json.loads(err)['error']
        assert rc == 1 and not out and not seen and not root.exists()
        assert error['code'] == 'ENGINE_INSTALL_INSUFFICIENT_DISK'
        assert error['free_bytes'] == free and error['required_bytes'] > free
        assert '--root' in error['message']
    else:
        assert rc == 0 and seen == ['backend', 'install'] and not err
    rc, out, err = invoke(['install', 'vibecomfy-h3', '--root', str(root), '--dry-run'], capsys)
    disk = json.loads(out)['disk_check']
    assert rc == 0 and not err and disk == {'required_bytes': disk['required_bytes'], 'free_bytes': free,
                                           'status': status, 'basis': 'locked_model_bytes'}
    assert disk['required_bytes'] > 0 and not root.exists()


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


def test_comfyui_linux_dry_run_resolves_packaged_recipe_and_model_lock(capsys):
    code, out, err = invoke(
        ['install', 'comfyui', '--platform', 'linux-x86_64-cu130', '--root', '/tmp/astrid-comfyui-resolution', '--dry-run'],
        capsys,
    )
    data = json.loads(out)
    assert code == 0 and not err
    assert data['recipe']['engine'] == 'comfyui'
    assert data['recipe']['platform'] == 'linux-x86_64-cu130'
    assert data['recipe']['environments'][0]['requirements']['packaged_lock'] == {
        'path': 'packs/comfyui/requirements.linux-cu130.lock',
        'sha256': '75f69f564bab7b45e50cb11ef5b2a5bade979e4dd76400393404c0d211a670a7',
    }
    assert data['recipe']['models_lock']['path'] == 'packs/comfyui/models.lock.json'
    assert data['plan']['models_lock_sha256'] == data['recipe']['models_lock']['sha256']
    assert data['recipe']['start']['argv'][-1] == '{models_config}'


@pytest.mark.parametrize('placement', ['local', 'runpod'])
def test_target_planning(placement, capsys):
    code, out, _ = invoke(['install', *BASE, '--placement', placement, '--dry-run'], capsys)
    assert code == 0 and json.loads(out)['plan']['placement'] == placement


def test_install_cache_argument_reaches_installer_without_changing_local_root(monkeypatch, capsys):
    seen = {}
    def backend(**kwargs):
        seen['backend'] = kwargs
        return object(), object()
    def install(plan, **kwargs):
        seen['plan'], seen['install'] = plan, kwargs
        return InstallResult(status='installed')
    monkeypatch.setattr(engine_cli, 'install_engine', install)
    rc, out, err = invoke(['install', *BASE, '--model-cache-root', '/mounted/models'], capsys,
                          backend_factory=backend)
    assert rc == 0 and not err and json.loads(out)['status'] == 'installed'
    assert seen['install']['model_cache_root'] == '/mounted/models'
    assert seen['plan'].root == seen['backend']['root'] == ROOT
    assert 'model_cache_root' not in seen['backend']
    assert recorded_engine_root('vibecomfy-h3') == ROOT


def test_model_cache_is_an_install_only_argument(capsys):
    with pytest.raises(SystemExit) as caught:
        engine_cli.main(['status', *BASE, '--model-cache-root', '/mounted/models'])
    assert caught.value.code == 2
    capsys.readouterr()


@pytest.mark.parametrize(('cache', 'placement', 'error'), [
    (ROOT, 'local', 'unsafe_path'),
    (ROOT + '/models', 'local', 'unsafe_path'),
    ('/TARGET_ONLY', 'local', 'unsafe_path'),
    ('relative/cache', 'local', 'unsafe_path'),
    ('/mounted/models', 'runpod', 'unsupported_placement'),
])
def test_invalid_cache_binding_refuses_before_backend(cache, placement, error, capsys):
    rc, out, err = invoke(['install', *BASE, '--placement', placement, '--model-cache-root', cache],
                          capsys, backend_factory=lambda **kw: pytest.fail('backend'))
    assert rc == 1 and not out and json.loads(err)['error']['code'] == error


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


def test_status_json_retains_integrity_gate_without_raw_target_output(monkeypatch, capsys):
    fixture = _runner_fixture()
    context = fixture.FakeContext()
    import astrid.core.generation.engine_platforms as platforms
    monkeypatch.setattr(platforms, 'platform_adapter', lambda *_: context.adapter)
    def backend(**kwargs):
        context.deadline = kwargs['deadline']
        return context, context.adapter
    rc, _, err = invoke(['install', *BASE], capsys, backend_factory=backend)
    assert rc == 0 and not err
    rc, out, err = invoke(['status', *BASE], capsys, backend_factory=backend)
    assert rc == 0 and not err
    assert json.loads(out)['status'] == 'installed' and json.loads(out)['failed_step_id'] is None
    context.sources[ROOT + '/runtime/ComfyUI']['dirty'] = '/private/PRIVATE_INPUT_SENTINEL'
    rc, out, err = invoke(['status', *BASE], capsys, backend_factory=backend)
    data = json.loads(out)
    assert rc == 0 and not err
    assert (data['status'], data['error_code'], data['failed_step_id']) == (
        'failed', 'integrity_failed', 'source:comfyui')
    assert '/private/' not in out and 'PRIVATE_INPUT_SENTINEL' not in out


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
    run_roots = []
    def status(engine, **kwargs):
        run_roots.append(kwargs['root'])
        return SimpleNamespace(status='installed', health=HealthEvidence('healthy', '2026-10-28T00:00:00Z', 'ab' * 32, True))
    for root in roots:
        rc, _, err = invoke(['install', 'vibecomfy-h3', '--root', root], capsys, backend_factory=backend)
        assert rc == 0, err
        assert recorded_engine_root('vibecomfy-h3') == root
        rc, _, err = invoke(['status', 'vibecomfy-h3'], capsys, backend_factory=backend)
        assert rc == 0, err
        receipt = Path(root) / '.astrid-engine' / 'install.json'
        receipt.parent.mkdir(parents=True, mode=0o700)
        receipt.write_text('{}')
        local = local_engine_endpoint(context=object(), status=status)
        assert run_roots[-1] == root
        assert local.endpoint.attestation.install_receipt_path == str(receipt)

    rc, _, err = invoke(['install', 'vibecomfy-h3'], capsys, backend_factory=backend)
    assert rc == 0, err
    assert seen == [roots[0], roots[0], roots[1], roots[1], roots[1]]
    assert run_roots == roots


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


def test_implicit_cli_root_prefers_record_to_environment(monkeypatch, tmp_path, capsys):
    from astrid.core.generation.engine_local_endpoint import record_engine_root

    registered = str(tmp_path / 'registered')
    record_engine_root('vibecomfy-h3', registered)
    override = str(tmp_path / 'override')
    monkeypatch.setenv('ASTRID_ENGINE_ROOT', override)
    rc, out, err = invoke(['install', 'vibecomfy-h3', '--dry-run'], capsys)
    assert rc == 0, err
    assert json.loads(out)['plan']['root'] == registered


def test_only_successful_explicit_local_installs_change_registration(monkeypatch, tmp_path, capsys):
    from astrid.core.generation.engine_local_endpoint import record_engine_root

    engine = 'vibecomfy-h3'
    original = str(tmp_path / 'original')
    other = str(tmp_path / 'other')
    record_engine_root(engine, original)
    seen = []
    def backend(**kwargs):
        seen.append(kwargs['root'])
        return object(), object()
    monkeypatch.setattr(engine_cli, 'install_engine', lambda *args, **kwargs: InstallResult(status='failed'))
    rc, _, _ = invoke(['install', engine, '--root', other], capsys, backend_factory=backend)
    assert rc == 0 and recorded_engine_root(engine) == original
    def fail(*args, **kwargs):
        raise CommandFailed('install failed')
    monkeypatch.setattr(engine_cli, 'install_engine', fail)
    rc, _, _ = invoke(['install', engine, '--root', other], capsys, backend_factory=backend)
    assert rc == 1 and recorded_engine_root(engine) == original
    monkeypatch.setattr(engine_cli, 'engine_status', lambda *args, **kwargs: None)
    rc, _, _ = invoke(['status', engine, '--root', other], capsys, backend_factory=backend)
    assert rc == 0 and seen == [other, other, other]
    assert recorded_engine_root(engine) == original

    # An implicit successful install uses a fallback but must not create a record.
    (Path.home() / '.astrid-data' / 'engines' / f'{engine}.root.json').unlink()
    monkeypatch.setenv('ASTRID_ENGINE_ROOT', other)
    monkeypatch.setattr(engine_cli, 'install_engine', lambda *args, **kwargs: InstallResult(status='installed'))
    rc, _, _ = invoke(['install', engine], capsys, backend_factory=backend)
    assert rc == 0 and seen[-1] == other and recorded_engine_root(engine) is None


def test_authenticated_cache_binding_reaches_installer(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(engine_cli, 'install_engine',
                        lambda plan, **kwargs: seen.update(kwargs) or InstallResult(status='installed'))
    rc, out, err = invoke(['install', *BASE, '--model-cache-root', '/mounted/models',
                          '--model-cache-device', '99', '--model-cache-mount-id', '71'], capsys,
                         backend_factory=lambda **kw: (object(), object()))
    assert rc == 0 and not err
    assert seen['model_cache_device'] == 99 and seen['model_cache_mount_id'] == 71


@pytest.mark.parametrize('flags', [
    ['--model-cache-device', '99'], ['--model-cache-mount-id', '71'],
    ['--model-cache-root', '/mounted/models', '--model-cache-device', '99'],
    ['--model-cache-root', '/mounted/models', '--model-cache-device', '-1', '--model-cache-mount-id', '71'],
])
def test_invalid_cache_binding_stops_before_backend(capsys, flags):
    rc, out, err = invoke(['install', *BASE, *flags], capsys,
                         backend_factory=lambda **kw: pytest.fail('unbound cache backend'))
    assert rc == 1 and not out and json.loads(err)['error']['code'] == 'unsafe_path'
