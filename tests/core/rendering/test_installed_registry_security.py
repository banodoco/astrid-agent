"""Review-only guard checks; no children, installation, or native execution."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.rendering import remotion_runtime as runtime
from astrid.packs.rendering.rendering.renderers.remotion import lock, run
from tests.core.rendering.test_remotion_registry_context_key import (
    _bind_installed_fixture,
    _completed_probe_receipt,
    _installed_observation,
)


@pytest.mark.parametrize('alias', [False, True], ids=['foreign-tree', 'symlink-alias'])
def test_installed_probe_must_refuse_unbound_tree_or_alias(tmp_path, monkeypatch, alias):
    observation = _installed_observation(tmp_path / 'foreign', sys.executable)
    if alias:
        link = tmp_path / 'alias'
        link.symlink_to(tmp_path / 'foreign/site-packages', target_is_directory=True)
        for key in ('astrid_origin', 'runtime_origin', 'generator_origin'):
            observation[key] = str(link / Path(observation[key]).relative_to(tmp_path / 'foreign/site-packages'))
    observation['probe_cwd'] = str(tmp_path)
    monkeypatch.setattr(runtime.tempfile, 'gettempdir', lambda: str(tmp_path))
    monkeypatch.setattr(runtime, '_owned_registry_probe', lambda *_a, **_k: (
        SimpleNamespace(returncode=0, stdout=json.dumps(observation)), _completed_probe_receipt()))
    with pytest.raises(runtime.RemotionValidationError):
        runtime._registry_probe({'ASTRID_HOST_EXECUTION_PROFILE': 'installed'}, sys.executable)


def test_installed_missing_carrier_must_fail_in_live_settler(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRID_HOST_EXECUTION_PROFILE', 'installed')
    monkeypatch.setattr(lock, 'remotion_render_lock_held', lambda: True)
    monkeypatch.setattr(run, 'gen_effect_registry', None)
    monkeypatch.setattr(run, '_registry_output_paths', lambda _: ())
    monkeypatch.setattr(runtime, '_registry_context', lambda env, **_: dict(env))
    with pytest.raises(runtime.RemotionValidationError, match='generator carrier is unavailable'):
        runtime._settle_registries(tmp_path, {'host': {}}, sys.executable)


def test_installed_generator_must_preserve_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRID_HOST_EXECUTION_PROFILE', 'installed')
    monkeypatch.setattr(lock, 'remotion_render_lock_held', lambda: True)
    monkeypatch.setattr(lock, '_resolved_lock_path', lambda: tmp_path / 'lock')
    monkeypatch.setattr(lock, 'remotion_render_lock_child_env', lambda: {})
    monkeypatch.setattr(run, 'gen_effect_registry', object())
    monkeypatch.setattr(run, '_registry_output_paths', lambda _: ())
    monkeypatch.setattr(run, '_read_registry_state', lambda _: None)
    monkeypatch.setattr(run, '_registry_output_content_hashes', lambda _: {})
    monkeypatch.setattr(runtime, '_registry_context', lambda env, **_: dict(env))
    monkeypatch.setattr(runtime, '_registry_inputs', lambda *_a, **_k: {})
    generator = tmp_path / 'generator.py'
    generator.write_text('# not executed\n')
    observation = {'state': {'content_hashes': {'effects': 'expected'}},
                   'lock_path': str(tmp_path / 'lock'), 'generator_origin': str(generator)}
    monkeypatch.setattr(runtime, '_registry_probe', lambda *_a, **_k: (observation, _completed_probe_receipt()))

    def inspect(argv, **kwargs):
        assert '-I' in argv[1:argv.index('-c')], f'installed generator lost isolation: {argv}'
        raise RuntimeError('stop before executing any generator')

    monkeypatch.setattr(runtime, '_owned_registry_probe', inspect)
    with pytest.raises(RuntimeError, match='stop before executing'):
        runtime._settle_registries(tmp_path, {'host': {}}, sys.executable)


@pytest.mark.parametrize('node', [None, '/selected/node'])
def test_installed_native_readiness_context_must_match_projected_context(tmp_path, monkeypatch, node):
    from astrid.core.execution import generic_host as host_module
    from astrid.core.subprocess_env import _SAFE_BASE_ENV
    from tests.test_generic_host import _write_manifest

    monkeypatch.setenv('ASTRID_HOST_EXECUTION_PROFILE', 'installed')
    environment = {'HOME': str(tmp_path), 'LANG': 'C', 'PATH': '/usr/bin',
                   'ASTRID_HOST_EXECUTION_PROFILE': 'installed'}
    monkeypatch.delenv('ASTRID_NODE_EXECUTABLE', raising=False)
    if node:
        environment['ASTRID_NODE_EXECUTABLE'] = node
    if schema_path := os.environ.get('ASTRID_TIMELINE_SCHEMA_PYTHONPATH'):
        environment['ASTRID_TIMELINE_SCHEMA_PYTHONPATH'] = schema_path
    for name in _SAFE_BASE_ENV - environment.keys():
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    manifest = _write_manifest(tmp_path / 'worker')
    data = json.loads(manifest.read_text())
    data['metadata']['requires_remotion'] = True
    manifest.write_text(json.dumps(data))
    host = host_module.GenericPackHost(pack_roots=[tmp_path], credential_source=environment)
    record = host.discover()[0]
    projected = host_module._launch_worker_probe_environments((tmp_path,), environment)
    contexts = [runtime._registry_context(env, generator=False) for env in projected]
    observed = []
    monkeypatch.setattr(runtime, '_validated_runtime', lambda _project, *, environment: observed.append(
        runtime._registry_context(environment, generator=False)))
    monkeypatch.setenv('ASTRID_REMOTION_VALIDATION_ROLE', 'worker')
    monkeypatch.setenv('ASTRID_REMOTION_PROJECT_DIR', str(tmp_path))
    _, secrets = host._child_environment(record, tmp_path / 'attempt', readiness_worker_kind='native')
    secrets.clear()
    assert observed and observed[0] in contexts
    assert observed[0].get('ASTRID_NODE_EXECUTABLE') == node


@pytest.mark.parametrize('field', ['runtime_origin', 'generator_origin', 'runtime_dependency_origins'])
def test_selected_installed_tree_refuses_foreign_carriers(tmp_path, monkeypatch, field):
    observation = _installed_observation(tmp_path, sys.executable)
    foreign = _installed_observation(tmp_path / 'foreign', sys.executable)
    _bind_installed_fixture(tmp_path, monkeypatch)
    observation[field] = foreign[field]
    monkeypatch.setattr(runtime.tempfile, 'gettempdir', lambda: str(tmp_path))
    monkeypatch.setattr(runtime, '_owned_registry_probe', lambda *_a, **_k: (
        SimpleNamespace(returncode=0, stdout=json.dumps(observation)), _completed_probe_receipt()))
    with pytest.raises(runtime.RemotionValidationError):
        runtime._registry_probe({'ASTRID_HOST_EXECUTION_PROFILE': 'installed'}, sys.executable)


def _preflight_tree(tmp_path):
    from astrid.core import source_identity

    root = tmp_path / 'installed'
    paths = (
        'astrid/__init__.py', 'astrid/version.py', 'astrid/__main__.py',
        'astrid/omp_agent.py', 'astrid/runtime_cli.py', 'astrid/core/__init__.py',
        'astrid/core/rendering/__init__.py', 'astrid/core/rendering/remotion_runtime.py',
        'astrid/core/rendering/_effect_registry.py', 'astrid/core/execution/generic_host.py',
        'astrid/sdk/__init__.py', 'astrid_runtime_client/__init__.py',
        'astrid/packs/__init__.py',
        'astrid/packs/rendering/rendering/renderers/remotion/__init__.py',
        'astrid/packs/rendering/rendering/renderers/remotion/run.py',
        'runtime_protocol/__init__.py', 'runtime_protocol/platform.py',
        'astrid_runtime/__init__.py',
    )
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise AssertionError('carrier executed before preflight')\n")
    (root / 'astrid/core/source_identity.py').write_bytes(Path(source_identity.__file__).read_bytes())
    return root, source_identity.source_checkout_closure_digest(root)


def _preflight(monkeypatch, root, digest, *, search=None):
    source = runtime._registry_origin._PREFLIGHT
    flags = SimpleNamespace(**{name: getattr(sys.flags, name) for name in dir(sys.flags)
                               if not name.startswith('_')})
    flags.isolated = 1
    with monkeypatch.context() as child:
        child.setattr(sys, 'flags', flags)
        child.setattr(sys, 'argv', ['-c', str(root), digest])
        child.setattr(sys, 'path', list(search or [str(root)]))
        for name in tuple(sys.modules):
            if any(name == product or name.startswith(product + '.')
                   for product in ('astrid', 'runtime_protocol', 'astrid_runtime')):
                child.delitem(sys.modules, name)
        before = sys.path[:]
        exec(source, {})
        assert sys.path == before


def test_installed_preflight_uses_existing_closure_without_importing_carriers(tmp_path, monkeypatch):
    root, digest = _preflight_tree(tmp_path)
    _preflight(monkeypatch, root, digest)


def test_installed_launch_binding_uses_selected_distribution_instead_of_sdk_caller_tree(tmp_path):
    root, digest = _preflight_tree(tmp_path)
    for generate in (False, True):
        argv = runtime._registry_origin.installed_argv('/selected/python', runtime.__file__,
            generate=generate, installed_source=(str(root), digest))
        assert argv[:5] == ['/selected/python', '-X', 'utf8', '-I', '-c']
        assert argv[argv.index('-c') + 2:] == [str(root), digest]
        assert str(runtime.REPO_ROOT) not in argv
        assert 'sys.path.insert' not in argv[argv.index('-c') + 1] and 'sys.path[0]' not in argv[argv.index('-c') + 1]
    with pytest.raises(ValueError, match='launch source closure changed'):
        runtime._registry_origin.installed_argv('/selected/python', runtime.__file__,
            installed_source=(str(root), '0' * 64))


def test_installed_sdk_validation_passes_existing_launch_closure(tmp_path, monkeypatch):
    from astrid.sdk import host_bootstrap

    root, digest = _preflight_tree(tmp_path)
    monkeypatch.setenv('ASTRID_HOST_EXECUTION_PROFILE', 'installed')
    captured = {}
    monkeypatch.setattr(runtime, 'prepare_remotion_validation', lambda **kwargs: captured.update(kwargs) or {})
    environment = {'ASTRID_REMOTION_PROJECT_DIR': str(tmp_path / 'remotion')}
    host_bootstrap._prepare_render_validation(root, environment, tmp_path, '/selected/python', (), digest)
    assert captured['python_executable'] == '/selected/python'
    assert captured['installed_source'] == (str(root), digest)


@pytest.mark.parametrize('mutation', [
    'foreign-astrid', 'foreign-runtime', 'generator-link', 'generator-ancestor',
    'root-alias', 'missing-generator', 'changed-generator', 'changed-runtime',
])
def test_installed_preflight_refuses_before_product_imports(tmp_path, monkeypatch, mutation):
    root, digest = _preflight_tree(tmp_path)
    search = [str(root)]
    generator = root / 'astrid/core/rendering/_effect_registry.py'
    if mutation.startswith('foreign-'):
        foreign, _ = _preflight_tree(tmp_path / 'foreign')
        if mutation == 'foreign-runtime':
            import shutil
            shutil.rmtree(foreign / 'astrid')
        search.insert(0, str(foreign))
    elif mutation == 'generator-link':
        target = root / 'foreign-generator.py'
        generator.rename(target)
        generator.symlink_to(target)
    elif mutation == 'generator-ancestor':
        directory = generator.parent
        target = directory.with_name('aliased-rendering')
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    elif mutation == 'root-alias':
        alias = tmp_path / 'alias'
        alias.symlink_to(root, target_is_directory=True)
        root = alias
        search = [str(alias)]
    elif mutation == 'missing-generator':
        generator.unlink()
    else:
        path = generator if mutation == 'changed-generator' else root / 'runtime_protocol/platform.py'
        path.write_text("raise AssertionError('changed carrier executed')\n")
    with pytest.raises(ValueError):
        _preflight(monkeypatch, root, digest, search=search)


@pytest.mark.parametrize('kind', ['native', 'command'])
@pytest.mark.parametrize('node', [None, '/selected/node'])
def test_installed_actual_worker_environment_matches_launch_projection(tmp_path, monkeypatch, kind, node):
    from astrid.core.execution import generic_host as host_module
    from astrid.core.subprocess_env import _SAFE_BASE_ENV
    from tests.test_generic_host import _write_manifest

    environment = {'HOME': str(tmp_path), 'LANG': 'C', 'PATH': '/usr/bin',
                   'ASTRID_HOST_EXECUTION_PROFILE': 'installed'}
    monkeypatch.delenv('ASTRID_NODE_EXECUTABLE', raising=False)
    if node:
        environment['ASTRID_NODE_EXECUTABLE'] = node
    if schema_path := os.environ.get('ASTRID_TIMELINE_SCHEMA_PYTHONPATH'):
        environment['ASTRID_TIMELINE_SCHEMA_PYTHONPATH'] = schema_path
    for name in _SAFE_BASE_ENV - environment.keys():
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    _write_manifest(tmp_path / 'worker')
    host = host_module.GenericPackHost(pack_roots=[tmp_path], credential_source=environment)
    record = host.discover()[0]
    monkeypatch.setattr(host_module, '_network_policy', lambda _: None)
    projected = host_module._launch_worker_probe_environments((tmp_path,), environment)
    observed = []

    class ReachedLaunch(Exception):
        pass

    def launch(_argv, **kwargs):
        observed.append(dict(kwargs['env']))
        raise ReachedLaunch

    monkeypatch.setattr(host_module, 'spawn_task_scope', launch)
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    with pytest.raises(ReachedLaunch):
        if kind == 'command':
            host._run_command_definition(record, {}, attempt / 'outputs', attempt)
        else:
            host.invoke_capability(capability_kind='executor', capability_id=record.id,
                                   request={}, attempt=attempt, child_env=environment)
    assert len(observed) == 1
    actual = observed[0]
    candidate = projected[1 if kind == 'native' else 2]
    assert actual.get('ASTRID_NODE_EXECUTABLE') == candidate.get('ASTRID_NODE_EXECUTABLE') == node
    assert 'PYTHONPATH' not in actual and 'ASTRID_PACKS_PATH' not in actual
    assert runtime.probe_context(environment=actual, python_executable=sys.executable) == runtime.probe_context(
        environment=candidate, python_executable=sys.executable)
    assert runtime._registry_context(actual, generator=False) == runtime._registry_context(candidate, generator=False)


def test_installed_render_worker_foreign_roots_refuse_before_publication(tmp_path, monkeypatch):
    from astrid.core.execution import generic_host as host_module
    from tests.test_generic_host import _write_manifest

    monkeypatch.setenv('ASTRID_HOST_EXECUTION_PROFILE', 'installed')
    manifest = _write_manifest(tmp_path / 'worker')
    data = json.loads(manifest.read_text())
    data['metadata']['requires_remotion'] = True
    manifest.write_text(json.dumps(data))
    with pytest.raises(host_module.HostError, match='product-owned pack roots'):
        host_module._launch_worker_probe_environments((tmp_path,), {'HOME': str(tmp_path)}, remotion_only=True)


@pytest.mark.parametrize('pack,capability', [
    ('capture', 'capture.web_page'), ('rendering', 'rendering.sprite_sheet'),
])
def test_installed_product_network_workers_remain_projectable(tmp_path, monkeypatch, pack, capability):
    from astrid.core.execution import generic_host as host_module
    from astrid.core.foundation.paths import REPO_ROOT

    monkeypatch.setenv('ASTRID_HOST_EXECUTION_PROFILE', 'installed')
    environment = {'HOME': str(tmp_path), 'LANG': 'C', 'PATH': '/usr/bin'}
    matrix_path = REPO_ROOT / 'config/astrid-beta-capabilities.json'
    owner = host_module.GenericPackHost(
        pack_roots=[REPO_ROOT / 'astrid/packs' / pack], capability_matrix=matrix_path,
    )
    record = next(record for record in owner.discover() if record.id == capability)
    assert host_module._network_policy(record) is not None
    assert record.matrix['disposition'] not in {'retired', 'unsupported'}
    assert host_module._record_may_render(record)
    projected = host_module._launch_worker_probe_environments(
        (REPO_ROOT / 'astrid/packs' / pack,), environment, matrix_path,
        remotion_only=True, installed_root=REPO_ROOT,
    )
    assert projected


@pytest.mark.parametrize('name', ['PYTHONPATH', 'ASTRID_PACKS_PATH'])
def test_installed_context_refuses_even_restatements_of_default_roots(monkeypatch, name):
    monkeypatch.setenv('ASTRID_HOST_EXECUTION_PROFILE', 'installed')
    raw = str(runtime.REPO_ROOT if name == 'PYTHONPATH' else runtime.REPO_ROOT / 'astrid/packs')
    with pytest.raises(runtime.RemotionValidationError, match='refuses explicit'):
        runtime._registry_context({name: raw}, generator=False)
