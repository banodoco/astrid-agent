"""Actual whole-wheel archive tests; generated archives are incomplete fixtures."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from astrid.core.generation import engine_release as release

REPO = Path(__file__).resolve().parents[3]
BASES = {
    'astrid': 'e9340851c165a157a2dbdd4764cc446e44e3b69f',
    'astrid-runtime': '470f6f4f49de3318e0e57cda19ca6dd666cd5cc4',
    'runpod-lifecycle': '29dae06e0ce8f350aac6586b178db6bebeae44b1',
    'runpod': '0786b3d51855a66176fea1e7443b92e7ef2d01b5',
}


@pytest.fixture(scope='module')
def wheel_inputs(tmp_path_factory):
    work = tmp_path_factory.mktemp('whole-wheel-inputs')
    local = {'astrid': REPO, 'astrid-runtime': REPO.parent / 'runtime',
             'runpod-lifecycle': REPO.parent / 'lifecycle', 'runpod': REPO.parent / 'sdk'}
    conventional = {'astrid-runtime': 'astrid-runtime', 'runpod-lifecycle': 'runpod-lifecycle',
                    'runpod': 'runpod-python'}
    for name in conventional:
        if not (local[name] / 'pyproject.toml').exists():
            local[name] = REPO.parent / conventional[name]
    assert all((p / 'pyproject.toml').is_file() for p in local.values()), 'frozen sibling source checkouts required'
    wheel_dir = work / 'wheels'
    wheel_dir.mkdir()
    env = dict(os.environ, UV_CACHE_DIR='/home/user/proof/.uv-cache',
               PIP_CACHE_DIR='/home/user/proof/.pip-cache',
               SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RUNPOD='1.12.0+astrid.2')
    built = subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                            '--wheel-dir', str(wheel_dir), *map(str, local.values())],
                           env=env, capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    wheels = sorted(wheel_dir.glob('*.whl'))
    provenance = {}
    for name, source in local.items():
        commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', BASES[name]], text=True).strip()
        delta = subprocess.check_output(['git', '-C', str(source), 'diff', '--binary', commit])
        normal = name.replace('-', '_') + '-'
        wheel = next(w for w in wheels if w.name.startswith(normal))
        provenance[name] = {'commit': commit, 'patches': [] if not delta else [
            {'name': name + '-additional.patch', 'sha256': hashlib.sha256(delta).hexdigest()}],
            'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}
    return work, wheels, provenance


@pytest.fixture(scope='module')
def archive_fixture(wheel_inputs):
    work, wheels, provenance = wheel_inputs
    archive, manifest = release.build_release(wheels, provenance, work / 'archives', fixture=True)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    root = release.extract_release(archive, sha, work / 'retained')
    return archive, manifest, sha, root


def test_actual_whole_wheels_build_extract_installed_canonical_dryrun(archive_fixture):
    archive, manifest, sha, root = archive_fixture
    assert archive.name == 'sha256-' + sha + '.zip'
    assert manifest['fixture'] is True and manifest['complete'] is False
    assert manifest['python'] == release.interpreter_identity()
    recipe = json.loads((REPO / 'astrid/packs/runpod/recipe.linux-cu130.json').read_text())
    for env in recipe['environments']:
        if env['requirements']['mode'] != 'packaged':
            assert 'resolved-linux-lock:' + env['name'] in manifest['missing']
        else:
            ref = env['requirements']['packaged_lock']
            assert 'resolved-linux-lock:' + env['name'] not in manifest['missing']
            bundled = root / 'site/astrid' / ref['path']
            assert hashlib.sha256(bundled.read_bytes()).hexdigest() == ref['sha256']
            assert bundled.read_bytes() == (REPO / 'astrid' / ref['path']).read_bytes()
    if not (REPO / 'astrid/core/generation/engine_custody.py').exists():
        assert 'module:astrid/core/generation/engine_custody.py' in manifest['missing']
    assert any(m.startswith('dependency:') for m in manifest['missing'])
    # The shared pre-CLI bootstrap helper travels in the verified release.
    assert 'module:astrid/core/generation/_engine_python_bootstrap.py' not in manifest['missing']
    assert (root / 'site/astrid/core/generation/_engine_python_bootstrap.py').read_bytes() == (
        REPO / 'astrid/core/generation/_engine_python_bootstrap.py').read_bytes()
    # Whole ordinary packages and original initializers, never source stubs.
    for path in ('site/astrid/omp_agent.py', 'site/astrid/installer/backend.py',
                 'site/runtime_protocol/platform.py', 'site/runpod_lifecycle/pod_agent.py',
                 'site/runpod/api/ctl_commands.py'):
        assert (root / path).is_file()
    assert (root / 'site/astrid/__init__.py').read_bytes() == (REPO / 'astrid/__init__.py').read_bytes()
    result = subprocess.run([sys.executable, '-I', '-S', str(root / 'run.py'), 'cli',
                             'engines', 'install', 'vibecomfy-h3', '--placement', 'local', '--dry-run'],
                            cwd=root.parent, env={'PATH': os.defpath, 'PYTHONPATH': '/nonexistent'},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert {step['kind'] for step in plan['plan']['steps']} == {'source', 'environment', 'model', 'start', 'health'}
    assert release.verify_tree(root) == manifest
    assert release.extract_release(archive, sha, root.parent) == root


def test_production_build_refuses_incomplete_inputs(wheel_inputs):
    work, wheels, provenance = wheel_inputs
    with pytest.raises(release.ReleaseError, match='incomplete release'):
        release.build_release(wheels, provenance, work / 'refused')
    assert not list((work / 'refused').glob('sha256-*.zip'))


def test_fixture_cannot_launch_install_or_startup(archive_fixture):
    _, _, _, root = archive_fixture
    for args in (('cli', 'engines', 'install', 'vibecomfy-h3', '--placement', 'local'),
                 ('startup', '--root', '/workspace/test', '--attempt', 'test'), ('check',)):
        result = subprocess.run([sys.executable, '-I', '-S', str(root / 'run.py'), *args],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 1
        assert result.stderr.strip() == 'retained release execution refused'


def test_explicit_entrypoint_verifier_refuses_fixture_and_tamper(archive_fixture, tmp_path):
    archive, _, sha, _ = archive_fixture
    argv = release.startup_argv(archive=str(archive), sha256=sha,
                                retain=str(tmp_path / 'retained'), root=str(tmp_path / 'engine'),
                                attempt='fixture', volume=str(tmp_path.parent), python=sys.executable)
    assert argv[:4] == (sys.executable, '-I', '-S', '-c')
    # Provider entrypoint/start-command arrays compose the exact same argv.
    entrypoint, start_command = argv[:4], argv[4:]
    result = subprocess.run([*entrypoint, *start_command], capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and result.stderr.strip() == 'release verification/build failed'
    bad = tmp_path / 'bad.zip'
    bad.write_bytes(b'digest mismatch; never executed')
    args = list(argv)
    args[args.index('--archive') + 1] = str(bad)
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and result.stderr.strip() == 'release verification/build failed'
    assert not (tmp_path / 'engine').exists()


def test_archive_digest_tamper_refused_before_extraction(archive_fixture, tmp_path):
    archive, _, sha, _ = archive_fixture
    bad = tmp_path / 'bad.zip'
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 1
    bad.write_bytes(data)
    with pytest.raises(release.ReleaseError, match='digest'):
        release.extract_release(bad, sha, tmp_path / 'retained')
    assert not (tmp_path / 'retained').exists()


def test_retained_file_tamper_and_extra_file_refused(archive_fixture):
    archive, _, sha, root = archive_fixture
    target = root / 'site/astrid/console.py'
    original = target.read_bytes()
    try:
        target.chmod(0o600)
        target.write_bytes(original + b'\n# tampered\n')
        with pytest.raises(release.ReleaseError, match='differs'):
            release.extract_release(archive, sha, root.parent)
    finally:
        target.write_bytes(original)
        target.chmod(0o444)
    extra = root / 'unexpected.py'
    try:
        extra.write_text('raise SystemExit(9)')
        with pytest.raises(release.ReleaseError, match='differs'):
            release.verify_tree(root)
    finally:
        extra.unlink()


@pytest.mark.parametrize('name,mode', [('../escape', 0o100444), ('/absolute', 0o100444),
                                      ('site/link', 0o120777), ('site/../escape', 0o100444)])
def test_archive_member_attack_refused(tmp_path, name, mode):
    archive = tmp_path / 'attack.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        item = zipfile.ZipInfo(name)
        item.create_system = 3
        item.external_attr = mode << 16
        bundle.writestr(item, b'not executable')
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(release.ReleaseError):
        release.extract_release(archive, sha, tmp_path / 'retained')
    assert not (tmp_path / 'escape').exists()


def test_provenance_must_bind_all_product_wheels(wheel_inputs):
    work, wheels, provenance = wheel_inputs
    changed = json.loads(json.dumps(provenance))
    changed['astrid']['wheel_sha256'] = '0' * 64
    with pytest.raises(release.ReleaseError, match='provenance'):
        release.build_release(wheels, changed, work / 'wrong-provenance', fixture=True)


def test_manifest_exact_member_set_and_duplicate_refused(tmp_path):
    for duplicate in (False, True):
        path = tmp_path / f'bad-{duplicate}.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in [('release.json', b'{"files":{}}'), ('extra.py', b'extra')]:
                item = zipfile.ZipInfo(name)
                item.create_system = 3
                item.external_attr = 0o100444 << 16
                archive.writestr(item, data)
            if duplicate:
                with pytest.warns(UserWarning):
                    archive.writestr(item, data)
        with pytest.raises(release.ReleaseError):
            release.extract_release(path, hashlib.sha256(path.read_bytes()).hexdigest(), tmp_path / 'retained')
