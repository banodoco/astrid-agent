"""Isolated children keep their selected interpreter's stdlib ahead of packages."""
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host as host
from tests.core.execution import test_confinement_launch_integration as confinement
from tests.test_generic_host import _write_manifest

launch = confinement.launch


PROBE = '''
import dataclasses, json, sys
from pathlib import Path
@dataclasses.dataclass(slots=True)
class Value:
    number: int = 7
print(json.dumps(dict(origin=dataclasses.__file__, slots=Value.__slots__,
                     value=Value().number, path=sys.path, executable=sys.executable)))
'''


def isolated_paths():
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
                             'import json,sys; print(json.dumps(sys.path))'],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.fixture
def bootstrap(tmp_path, launch, monkeypatch):
    policy, _, _, outside = launch
    purelib = tmp_path / 'legacy-purelib'
    purelib.mkdir()
    # Model the pre-3.10 backport's signature, which rejects slots=True.
    (purelib / 'dataclasses.py').write_text(
        'def dataclass(cls=None, *, frozen=False):\n    return cls\n'
        'def asdict(value):\n    return vars(value)\n'
        'def is_dataclass(value):\n    return False\n')
    dependency = tmp_path / 'dependency'
    dependency.mkdir()
    get_path = host.sysconfig.get_path
    monkeypatch.setattr(host.sysconfig, 'get_path',
                        lambda name: str(purelib) if name == 'purelib' else get_path(name))
    monkeypatch.setattr(host, '_dependency_pythonpath', lambda: (str(dependency),))
    pack = tmp_path / 'pack'
    manifest = _write_manifest(pack)
    definition = json.loads(manifest.read_text())
    # Avoid braces in the command's format-template text.
    code = PROBE + '\nPath(sys.argv[1]).write_text(json.dumps(dict(origin=dataclasses.__file__, slots=Value.__slots__, value=Value().number, path=sys.path, executable=sys.executable)))\n'
    definition['command']['argv'] = ['{python_exec}', '-c', code, '{out}/answer.txt']
    manifest.write_text(json.dumps(definition))
    checkout = Path(host.__file__).resolve().parents[3]
    policy = replace(policy,
        bootstrap_files=(*policy.bootstrap_files,
            Path('/opt/homebrew/opt/libb2/lib/libb2.1.dylib'),
            Path('/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib'),
            Path('/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib')),
        bootstrap_roots=(*policy.bootstrap_roots, checkout, pack, purelib, dependency))
    h = host.GenericPackHost(pack_roots=[pack], confinement_policy=policy)
    h.discover()
    return SimpleNamespace(host=h, record=h.capabilities['test.echo'], policy=policy,
                           purelib=purelib, dependency=dependency, pack=pack,
                           checkout=checkout, outside=outside)


def assert_stdlib(observed):
    assert Path(observed['origin']).resolve() == (
        Path(host.sysconfig.get_path('stdlib')) / 'dataclasses.py').resolve()
    assert observed['slots'] == ['number'] and observed['value'] == 7
    assert Path(observed['executable']).resolve() == Path(sys.executable).resolve()


@pytest.mark.parametrize('entry', ['command', 'executor'])
def test_declared_startup_ignores_legacy_purelib_dataclasses(bootstrap, entry):
    f = bootstrap
    h, policy = f.host, f.policy
    definition, admission = h.admit('executor', 'test.echo')
    if entry == 'command':
        result = h._run_command_definition(f.record, {}, policy.private_write_root,
                                          policy.attempt_root, admission=admission)
        assert result.returncode == 0, result
    else:
        result = h.invoke_capability(capability_kind='executor', capability_id='test.echo',
            request=dict(out=str(policy.private_write_root), inputs={}, project='synthetic',
                         project_was_auto_resolved=True), attempt=policy.attempt_root,
            definition=definition, admission=admission)
        assert result.ok, result
    observed = json.loads((policy.private_write_root / 'answer.txt').read_text())
    assert_stdlib(observed)
    assert not h._active_processes


@pytest.mark.parametrize('entry', ['command', 'worker', 'renderer'])
def test_isolated_bootstrap_exact_path_order(bootstrap, monkeypatch, entry):
    f = bootstrap
    captured = []
    class Captured(Exception):
        pass
    def capture(argv, **kwargs):
        # Still compile/validate the real confinement plan before stopping.
        plan = compile_plan(argv, **kwargs)
        assert '(deny default)' in plan.argv[2]
        assert f'(allow file-read* (subpath "{f.outside.parent}"))' not in plan.argv[2]
        captured.append(list(argv))
        raise Captured
    compile_plan = f.host._workload_launch_plan
    monkeypatch.setattr(f.host, '_workload_launch_plan', capture)
    lease = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(f.host, '_renderer_task_lease', lambda *a, **kw: lease)
    with pytest.raises(Captured):
        if entry == 'worker':
            f.host.invoke_capability(capability_kind='executor', capability_id='test.echo',
                request={}, attempt=f.policy.attempt_root)
        else:
            record = replace(f.record, definition=replace(f.record.definition,
                id='rendering.timeline_visualize')) if entry == 'renderer' else f.record
            f.host._run_command_definition(record, {}, f.policy.private_write_root,
                                            f.policy.attempt_root)
    argv, = captured
    assert argv[:5] == [sys.executable, '-I', '-S', '-B', '-c']
    imports = [str(f.checkout), str(f.purelib), host.sysconfig.get_path('platlib')]
    if entry == 'command':
        imports += [str(f.pack), str(f.pack.parent)]
    imports += [str(f.dependency)]
    imports = list(dict.fromkeys(str(Path(p).resolve()) if entry == 'command' else p for p in imports))
    assert json.loads(argv[6]) == imports
    # Stop at the worker entry point: exercise the emitted startup verbatim,
    # without launching a renderer or giving the probe a provider protocol.
    if entry != 'command':
        argv[5] = 'import runpy; runpy.run_module=lambda *a, **kw: exec(' + repr(PROBE) + '); ' + argv[5]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    observed = json.loads(result.stdout)
    assert_stdlib(observed)
    assert observed['path'] == [*isolated_paths(), *imports]


@pytest.mark.parametrize('corruption', [None, 'changed-hook', 'changed-policy', 'shadow-hook'])
def test_network_bootstrap_keeps_interpreter_paths_first(tmp_path, corruption):
    hook = tmp_path / '.astrid-network-hook/sitecustomize.py'
    hook.parent.mkdir()
    hook.write_text(host._STRICT_NETWORK_HOOK_SOURCE)
    policy = tmp_path / 'policy.py'
    policy.write_text('def install_from_environment(**kwargs):\n    return True\n')
    dependency = tmp_path / 'dependency'
    dependency.mkdir()
    (dependency / 'dataclasses.py').write_text('raise RuntimeError("legacy dataclasses")\n')
    (dependency / 'probe.py').write_text(PROBE)
    argv = host._network_startup_argv(
        [sys.executable, '-m', 'astrid.core.execution.generic_host_worker'], tmp_path,
        {'PYTHONPATH': os.pathsep.join((str(hook.parent), str(dependency)))}, broker_required=False)
    config = json.loads(argv[-1])
    config.update(policy=str(policy), policy_sha256=hashlib.sha256(policy.read_bytes()).hexdigest(), module='probe')
    argv[-1] = json.dumps(config)
    if corruption == 'changed-hook':
        hook.write_text('raise RuntimeError("changed hook")\n')
    elif corruption == 'changed-policy':
        policy.write_text('raise RuntimeError("changed policy")\n')
    elif corruption == 'shadow-hook':
        (tmp_path / 'sitecustomize.py').write_text('raise RuntimeError("shadow hook")\n')
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)
    if corruption:
        assert result.returncode == 78 and result.stdout == ''
        assert result.stderr == 'astrid network startup failed\n'
        return
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert_stdlib(observed)
    stdlib = isolated_paths()
    assert observed['path'][:len(stdlib) + 3] == [*stdlib, str(tmp_path), str(hook.parent), str(dependency)]
    assert len(observed['path']) == len(set(observed['path']))


def test_render_backend_bootstrap_exact_path_order(bootstrap, monkeypatch):
    from astrid.core.execution import render_command_pipe as relay

    f = bootstrap
    owner = relay.HostRenderCommandBridge.__new__(relay.HostRenderCommandBridge)
    owner.policy, owner.environment = f.policy, {}
    owner.imports = [str(f.checkout), str(f.purelib),
                     host.sysconfig.get_path('platlib'), str(f.dependency)]
    captured = []
    compile_plan = host._confined_launch_plan
    class Captured(Exception):
        pass
    def capture(argv, **kwargs):
        plan = compile_plan(argv, **kwargs)
        assert '(deny default)' in plan.argv[2]
        captured.append(list(argv))
        raise Captured
    monkeypatch.setattr(host, '_confined_launch_plan', capture)
    with pytest.raises(Captured):
        owner._launch_locked([sys.executable, 'probe.py'],
                             cwd=str(f.policy.private_write_root), _backend=True)
    argv, = captured
    assert argv[:5] == [sys.executable, '-I', '-S', '-B', '-c']
    assert json.loads(argv[6]) == owner.imports
    # Substitute only the private pipe installer and backend entry point;
    # the emitted interpreter/path/environment setup still executes.
    argv[5] = (
        'import runpy,sys,types; '
        'pipe=types.ModuleType("astrid.core.execution.render_command_pipe"); '
        'pipe.install_render_command_client=lambda: None; '
        'sys.modules[pipe.__name__]=pipe; '
        'runpy.run_path=lambda *a, **kw: exec(' + repr(PROBE) + '); ' + argv[5])
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    observed = json.loads(result.stdout)
    assert_stdlib(observed)
    assert observed['path'] == [*isolated_paths(), *owner.imports]
