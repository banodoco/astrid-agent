"""Registry context keys collapse restatements of the default corpus, nothing else."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.rendering import remotion_runtime as runtime


@pytest.fixture(autouse=True)
def _reset_process_local_remotion_custody(monkeypatch):
    monkeypatch.setattr(runtime, '_UNRESOLVED_PROBE_CUSTODY', [])
    monkeypatch.setattr(runtime, '_UNRESOLVED_VALIDATION_PROBES', [])


def _env(tmp_path, **extra):
    return {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin', **extra}


def _key(env):
    return runtime._registry_context(env, generator=False)


@pytest.mark.parametrize('generator', [False, True])
def test_explicit_node_executable_is_preserved_and_changes_context(tmp_path, generator):
    base = _env(tmp_path)
    configured = _env(tmp_path, ASTRID_NODE_EXECUTABLE='/opt/node/bin/node')
    context = runtime._registry_context(configured, generator=generator)
    assert context['ASTRID_NODE_EXECUTABLE'] == '/opt/node/bin/node'
    assert context != runtime._registry_context(base, generator=generator)
    assert context != runtime._registry_context(
        _env(tmp_path, ASTRID_NODE_EXECUTABLE='/other/node'), generator=generator)


def _installed_observation(tmp_path, python):
    root = tmp_path / 'site-packages'
    astrid = root / 'astrid'
    runtime_origin = astrid / 'core/rendering/remotion_runtime.py'
    astrid_origin = astrid / '__init__.py'
    generator = astrid / 'core/rendering/_effect_registry.py'
    for path in (astrid_origin, runtime_origin, generator, *(Path(raw) for raw in _runtime_origins(root).values())):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# installed fixture\n', encoding='utf-8')
    return {
        'state': {'hash': 'a' * 64,
                  'content_hashes': {kind: 'b' * 64 for kind in runtime._REGISTRY_KINDS}},
        'astrid_origin': str(astrid_origin),
        'runtime_origin': str(runtime_origin),
        'generator_origin': str(generator),
        'runtime_dependency_origins': _runtime_origins(root),
        'probe_cwd': str(tmp_path.resolve()),
        'startup': {'executable': python},
    }


def _runtime_origins(root):
    return {name: str(root / path) for name, path in {
        'runtime_protocol': 'runtime_protocol/__init__.py',
        'runtime_protocol.platform': 'runtime_protocol/platform.py',
        'astrid_runtime': 'astrid_runtime/__init__.py',
    }.items()}


def _bind_installed_fixture(tmp_path, monkeypatch):
    root = tmp_path / 'site-packages'
    # The selected host tree is an independent fixture input, not inferred
    # from the observation being checked. No interpreter runs in these tests.
    monkeypatch.setattr(runtime._registry_origin, 'selected_root', lambda _: root)
    monkeypatch.setattr(runtime._registry_origin, 'source_checkout_closure_digest', lambda _: 'a' * 64)


def _completed_probe_receipt():
    return {
        'original_wait_exit_code': 0,
        'retained_set_final_census_complete': True,
        'custody_admitted': True,
    }


@pytest.mark.parametrize('generator', [False, True])
def test_source_probe_prefers_caller_tree_already_on_search_path(tmp_path, monkeypatch, generator):
    selected, checkout = tmp_path / 'site-packages', tmp_path / 'checkout'
    for root in (selected, checkout):
        for package in ('astrid', 'astrid/core', 'astrid/core/rendering'):
            init = root / package / '__init__.py'
            init.parent.mkdir(parents=True, exist_ok=True)
            init.write_text('')
        module = root / 'astrid/core/rendering/remotion_runtime.py'
        module.write_text(
            'import sys\n'
            'def _registry_observation():\n'
            f'    return {{"runtime_origin": __file__, "startup": {{"executable": sys.executable}}, '
            f'"state": {{"hash": "a", "content_hashes": {dict.fromkeys(runtime._REGISTRY_KINDS, "b" * 64)!r}}}}}\n'
        )
    (selected / 'scripts').mkdir()
    monkeypatch.delenv('ASTRID_HOST_EXECUTION_PROFILE', raising=False)
    monkeypatch.setattr(runtime, 'REPO_ROOT', selected)
    monkeypatch.setattr(runtime, '__file__', str(selected / 'astrid/core/rendering/remotion_runtime.py'))

    def owned(argv, *, cwd, env, phase):
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True), _completed_probe_receipt()

    monkeypatch.setattr(runtime, '_owned_registry_probe', owned)
    environment = {'PYTHONPATH': os.pathsep.join((str(checkout), str(selected)))}
    observation, _ = runtime._registry_probe(environment, sys.executable, generator=generator)
    assert observation['runtime_origin'] == runtime.__file__


def test_installed_registry_probe_uses_selected_interpreter_and_neutral_context(tmp_path, monkeypatch):
    python = '/selected/venv/bin/python'
    environment = runtime._registry_context(_env(tmp_path, ASTRID_HOST_EXECUTION_PROFILE='installed',
                                                ASTRID_NODE_EXECUTABLE='/selected/node'), generator=False)
    captured = {}
    observation = _installed_observation(tmp_path, python)
    _bind_installed_fixture(tmp_path, monkeypatch)

    def owned(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return type('Result', (), {'returncode': 0, 'stdout': json.dumps(observation), 'stderr': ''})(), _completed_probe_receipt()

    monkeypatch.setattr(runtime, '_owned_registry_probe', owned)
    monkeypatch.setattr(runtime.tempfile, 'gettempdir', lambda: str(tmp_path))
    result, _ = runtime._registry_probe(environment, python)

    assert result == observation
    assert captured['argv'][0] == python
    assert '-I' in captured['argv']
    assert REPO_ROOT.as_posix() not in captured['argv']
    assert captured['kwargs']['cwd'] == tmp_path
    assert captured['kwargs']['env']['ASTRID_NODE_EXECUTABLE'] == '/selected/node'
    assert 'PYTHONPATH' not in captured['kwargs']['env']
    assert 'ASTRID_PACKS_PATH' not in captured['kwargs']['env']


@pytest.mark.parametrize('field', ['executable', 'cwd'])
def test_installed_registry_probe_refuses_wrong_interpreter_or_foreign_cwd(tmp_path, monkeypatch, field):
    python = '/selected/venv/bin/python'
    environment = _env(tmp_path, ASTRID_HOST_EXECUTION_PROFILE='installed')
    observation = _installed_observation(tmp_path, python)
    _bind_installed_fixture(tmp_path, monkeypatch)
    if field == 'executable':
        observation['startup']['executable'] = '/foreign/python'
    else:
        observation['probe_cwd'] = str(tmp_path / 'foreign-cwd')

    monkeypatch.setattr(
        runtime, '_owned_registry_probe',
        lambda *_args, **_kwargs: (
            type('Result', (), {'returncode': 0, 'stdout': json.dumps(observation), 'stderr': ''})(),
            _completed_probe_receipt(),
        ),
    )
    monkeypatch.setattr(runtime.tempfile, 'gettempdir', lambda: str(tmp_path))
    with pytest.raises(runtime.RemotionValidationError, match='owned registry discovery failed'):
        runtime._registry_probe(environment, python)


@pytest.mark.parametrize('name', ['PYTHONPATH', 'ASTRID_PACKS_PATH'])
def test_installed_registry_probe_refuses_foreign_import_or_pack_roots(tmp_path, monkeypatch, name):
    environment = _env(tmp_path, ASTRID_HOST_EXECUTION_PROFILE='installed',
                        **{name: str(tmp_path / 'foreign')})
    monkeypatch.setattr(
        runtime, '_owned_registry_probe',
        lambda *_args, **_kwargs: pytest.fail('unsupported installed context was launched'),
    )

    with pytest.raises(runtime.RemotionValidationError, match='installed registry discovery refuses'):
        runtime._registry_probe(environment, '/selected/venv/bin/python')


def test_installed_registry_observation_reports_missing_packaged_generator(monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import run

    monkeypatch.setattr(run, 'gen_effect_registry', None)
    with pytest.raises(runtime.RemotionValidationError, match='generator carrier is unavailable'):
        runtime._registry_observation()


def test_pythonpath_naming_the_checkout_collapses_with_its_absence(tmp_path):
    base = _env(tmp_path)
    assert _key(_env(tmp_path, PYTHONPATH=str(REPO_ROOT))) == _key(base)
    assert 'PYTHONPATH' not in _key(_env(tmp_path, PYTHONPATH=str(REPO_ROOT)))


def test_a_different_pythonpath_entry_keeps_its_own_context(tmp_path):
    extra = tmp_path / 'deps'
    extra.mkdir()
    key = _key(_env(tmp_path, PYTHONPATH=os.pathsep.join((str(REPO_ROOT), str(extra)))))
    assert key['PYTHONPATH'] == str(extra)
    assert key != _key(_env(tmp_path))


def test_builtin_and_managed_pack_roots_collapse_but_an_external_root_does_not(tmp_path, monkeypatch):
    managed = tmp_path / 'managed-pack'
    managed.mkdir()
    external = tmp_path / 'external-pack'
    external.mkdir()
    from astrid.core.pack import source_setup

    class _Inventory:
        sources = [type('Source', (), {'pack_root': managed})()]
    monkeypatch.setattr(source_setup, 'active_source_inventory', lambda **_: _Inventory())
    builtin = str(REPO_ROOT / 'astrid' / 'packs')
    base = _key(_env(tmp_path))
    assert _key(_env(tmp_path, ASTRID_PACKS_PATH=builtin)) == base
    assert _key(_env(tmp_path, ASTRID_PACKS_PATH=os.pathsep.join((builtin, str(managed))))) == base
    different = _key(_env(tmp_path, ASTRID_PACKS_PATH=os.pathsep.join((builtin, str(external)))))
    assert different != base and different['ASTRID_PACKS_PATH'] == str(external)


def test_render_in_an_unvalidated_context_is_still_refused(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    external = tmp_path / 'external-pack'
    external.mkdir()
    reference = tmp_path / 'validation.json'
    raw = json.dumps({'schema': runtime._VALIDATION_SCHEMA, 'launch_id': 'l',
                      'probe_source_sha256': hashlib.sha256(runtime._schema_probe_source().encode()).hexdigest(),
                      'contexts': {'host': {'variants': []}}}).encode()
    reference.write_bytes(raw)
    reference.chmod(0o600)
    env = _env(tmp_path, ASTRID_PACKS_PATH=str(external))
    env.update(dict(zip(runtime._REF_ENV_KEYS, (str(reference), hashlib.sha256(raw).hexdigest(), 'l', 'host'))))
    with pytest.raises(runtime.RemotionValidationError, match='consumer context differs'):
        runtime._validated_runtime(project, environment=env, python_executable='/usr/bin/python3')
    env.pop(runtime._REF_ENV_KEYS[0])
    with pytest.raises(runtime.RemotionValidationError, match='missing or invalid launch validation'):
        runtime._validated_runtime(project, environment=env, python_executable='/usr/bin/python3')


def test_repeated_path_entries_do_not_make_a_different_context():
    # A shell that sourced its env twice (e.g. inside an otto-heavy job) repeats PATH
    # entries; lookup is unchanged, so the validated context must match.
    import os
    from astrid.core.rendering import remotion_runtime as rr
    base = {"HOME": "/h", "PATH": os.pathsep.join(["/v/bin", "/n/bin", "/usr/bin"]), "TMPDIR": "/t"}
    doubled = {**base, "PATH": os.pathsep.join(["/v/bin", "/n/bin", "/v/bin", "/n/bin", "/usr/bin"])}
    assert rr._dedupe_path(doubled["PATH"]) == base["PATH"]
    assert rr._registry_context(doubled, generator=False) == rr._registry_context(base, generator=False)
    import sys
    a = rr.probe_context(environment=doubled, python_executable=sys.executable)
    b = rr.probe_context(environment=base, python_executable=sys.executable)
    assert a["node_env"] == b["node_env"]
    # A genuinely different PATH order still differs.
    swapped = {**base, "PATH": os.pathsep.join(["/n/bin", "/v/bin", "/usr/bin"])}
    assert rr._registry_context(swapped, generator=False) != rr._registry_context(base, generator=False)


def test_windows_registry_context_keeps_windows_child_environment_only_on_windows(monkeypatch):
    windows = {name: f"value-{name}" for name in (
        "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
        "SYSTEMROOT", "TEMP", "TMP", "PATHEXT", "COMSPEC",
    )}
    monkeypatch.setattr(runtime, "probe_context", lambda **_: {})
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="win32", executable="python"))

    assert runtime._registry_context(windows, generator=True) == windows

    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="linux", executable="python"))
    assert runtime._registry_context(windows, generator=True) == {}
