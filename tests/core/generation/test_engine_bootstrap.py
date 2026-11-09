import base64
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from astrid.core.generation import engine_bootstrap as bootstrap
from astrid.core.generation.engine_contracts import IntegrityError, RecipeValidationError

ARGV = ('engines', 'install', 'comfyui', '--dry-run', '--root', '/tmp/astrid-plan')


def run(bundle, env=None, loader=None):
    command = list(bundle.command)
    if loader is not None:
        command[-1] = loader
    return subprocess.run(command, env={'PATH': os.environ['PATH'], **(env or bundle.env)},
                          text=True, capture_output=True, timeout=30)


def repack(bundle, archive=None, spec=None):
    env = dict(bundle.env)
    spec = spec or json.loads(base64.b64decode(env['ASTRID_ENGINE_SPEC']))
    if archive is not None:
        env['ASTRID_ENGINE_SOURCE'] = base64.b64encode(lzma.compress(archive)).decode()
        spec['source_sha256'] = hashlib.sha256(archive).hexdigest()
    raw = json.dumps(spec, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    old = hashlib.sha256(base64.b64decode(bundle.env['ASTRID_ENGINE_SPEC'])).hexdigest()
    loader = bundle.command[-1].replace(old, hashlib.sha256(raw).hexdigest())
    env['ASTRID_ENGINE_SPEC'] = base64.b64encode(raw).decode()
    return env, loader


def unzip(bundle):
    return lzma.decompress(base64.b64decode(bundle.env['ASTRID_ENGINE_SOURCE']))


def test_console_dry_run_and_help_are_isolated_and_exact():
    b = bootstrap.build_cli_bootstrap(ARGV)
    free_bytes = 2**63 - 1
    loader = b.command[-1].replace(
        "raise SystemExit(entries[0].load()())",
        "entry = entries[0].load()\n"
        "    print('ORIGIN=' + sys.modules['astrid.console'].__file__, file=sys.stderr)\n"
        "    import astrid.core.engine_cli as _cli\n"
        f"    _cli._disk_anchor = lambda root: (root, 1, 1, {free_bytes})\n"
        "    raise SystemExit(entry())",
    )
    result = run(b, loader=loader)
    assert result.returncode == 0, result.stderr
    assert 'ORIGIN=' in result.stderr and 'astrid-engine-cli-' in result.stderr
    plan = json.loads(result.stdout)
    assert {step['kind'] for step in plan['plan']['steps']} == {'source', 'environment', 'start', 'health'}
    # Plan 32 T5h: an install is model-free, so the plan quantifies no model bytes.
    assert plan['disk_check'] == {
        'required_bytes': 0, 'free_bytes': free_bytes,
        'status': 'ok', 'basis': 'model_free_install',
    }
    local_code = (
        f"import astrid.core.engine_cli as c; c._disk_anchor = lambda root: (root, 1, 1, {free_bytes}); "
        "from astrid.console import main; raise SystemExit(main())"
    )
    local = subprocess.run([sys.executable, '-c', local_code, *ARGV],
                           env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, text=True, capture_output=True)
    assert local.returncode == 0 and result.stdout == local.stdout
    help_result = run(bootstrap.build_cli_bootstrap(('engines', '--help')))
    assert help_result.returncode == 0 and 'astrid engines' in help_result.stdout
    assert not Path('/tmp/astrid-plan').exists()


def test_exact_bytes_determinism_and_identity():
    a = bootstrap.build_cli_bootstrap(ARGV)
    b = bootstrap.build_cli_bootstrap(ARGV)
    assert a == b
    with zipfile.ZipFile(io.BytesIO(unzip(a))) as z:
        for name in bootstrap._FILES:
            content = Path(name).read_bytes()
            assert z.read(name) == content
            assert a.files[name] == hashlib.sha256(content).hexdigest()
        assert z.read('astrid-0.1.0.dist-info/entry_points.txt').find(b'astrid = astrid.console:main') >= 0


def test_change_and_missing_source(monkeypatch, tmp_path):
    # Redirect only the explicit closure reads to a disposable tree.
    root = Path(bootstrap.__file__).resolve().parents[3]
    for name in bootstrap._FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
    fake = tmp_path / 'astrid/core/generation/engine_bootstrap.py'
    fake.write_text('')
    (fake.parent / '_engine_bootstrap_loader.py').write_bytes(Path(bootstrap.__file__).with_name('_engine_bootstrap_loader.py').read_bytes())
    import shutil
    shutil.copytree(root / 'astrid.egg-info', tmp_path / 'astrid.egg-info')
    monkeypatch.setattr(bootstrap, '__file__', str(fake))
    initial = bootstrap.build_cli_bootstrap(ARGV)
    for name in ('astrid/core/engine_cli.py', 'astrid/packs/runpod/pins.json'):
        path = tmp_path / name
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        assert bootstrap.build_cli_bootstrap(ARGV).source_sha256 != initial.source_sha256
        path.write_bytes(original)
    (tmp_path / bootstrap._FILES[0]).unlink()
    with pytest.raises(IntegrityError):
        bootstrap.build_cli_bootstrap(ARGV)


@pytest.mark.parametrize('argv', [(), ('status',), ('engines', 'install', 'comfyui'),
    ('engines', 'install', 'comfyui', '--dry-run', '--token', 'secret'),
    ('engines', 'install', 'comfyui', '--dry-run', '--root', '/x?token=secret'),
    ('engines', 'install', 'comfyui', '--dry-run', '--dry-run')])
def test_public_argv_refusal(argv):
    with pytest.raises(RecipeValidationError): bootstrap.build_cli_bootstrap(argv)


@pytest.mark.parametrize('name', ['_SOURCE_LIMIT', '_ARCHIVE_LIMIT', '_SPEC_LIMIT', '_TOTAL_LIMIT'])
def test_all_builder_caps(monkeypatch, name):
    monkeypatch.setattr(bootstrap, name, 1)
    with pytest.raises((IntegrityError, RecipeValidationError)): bootstrap.build_cli_bootstrap(ARGV)


def test_tampering_and_amplification():
    b = bootstrap.build_cli_bootstrap(ARGV)
    for key in b.env:
        env = dict(b.env); env[key] = env[key][:-4] + 'AAAA'
        assert run(b, env).returncode != 0
    env = dict(b.env)
    env['ASTRID_ENGINE_SOURCE'] = base64.b64encode(lzma.compress(b'x' * 1000000)).decode()
    assert run(b, env).returncode != 0
    env = dict(b.env); env['ASTRID_ENGINE_SOURCE'] = 'A' * 65540
    assert run(b, env).returncode != 0
    spec = json.loads(base64.b64decode(b.env['ASTRID_ENGINE_SPEC']))
    spec['files']['astrid/__init__.py'] = '0' * 64
    env, loader = repack(b, spec=spec)
    assert run(b, env, loader).returncode != 0


@pytest.mark.parametrize('kind', ['duplicate', 'unsafe', 'alias', 'symlink', 'oversize', 'missing'])
def test_bad_zip_members_refused(kind):
    b = bootstrap.build_cli_bootstrap(ARGV)
    original = unzip(b)
    with zipfile.ZipFile(io.BytesIO(original)) as src:
        entries = [(i, src.read(i)) for i in src.infolist()]
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as dst:
        for i, data in entries:
            if kind == 'missing' and i.filename == 'astrid/__init__.py': continue
            if i.filename == 'astrid/__init__.py':
                if kind == 'unsafe': i.filename = '../escape'
                if kind == 'alias': i.filename = 'astrid/./__init__.py'
                if kind == 'symlink': i.external_attr = 0o120777 << 16
                if kind == 'oversize': data = b'x' * 524289
            dst.writestr(i, data)
        if kind == 'duplicate': dst.writestr(entries[0][0], entries[0][1])
    spec = json.loads(base64.b64decode(b.env['ASTRID_ENGINE_SPEC']))
    if kind in ('unsafe', 'alias'):
        spec['files']['../escape' if kind == 'unsafe' else 'astrid/./__init__.py'] = spec['files'].pop('astrid/__init__.py')
    env, loader = repack(b, out.getvalue(), spec)
    assert run(b, env, loader).returncode != 0


def test_execution_is_not_qualified():
    with pytest.raises(RecipeValidationError):
        bootstrap.build_cli_bootstrap(('engines', 'install', 'comfyui'))


def test_real_console_execution_refuses_without_a2(tmp_path):
    # Historical name: the CLI now has a real backend, but this fixture stops at
    # the locked-model disk gate before backend/custody/install work can begin.
    b = bootstrap.build_cli_bootstrap(ARGV)
    spec = json.loads(base64.b64decode(b.env['ASTRID_ENGINE_SPEC']))
    root = tmp_path / 'uncreated-engine-root'
    assert not root.exists()
    spec['argv'] = ['engines', 'install', 'comfyui', '--root', str(root)]
    env, loader = repack(b, spec=spec)
    sentinel = tmp_path / 'operation-sentinel'
    loader = loader.replace(
        "raise SystemExit(entries[0].load()())",
        "import astrid.core.engine_cli as _cli\n"
        "    def _unexpected(*args, **kwargs):\n"
        f"        open({str(sentinel)!r}, 'w').write('entered')\n"
        "        raise RuntimeError('bootstrap fixture operation sentinel entered')\n"
        "    _cli._disk_anchor = lambda root: (root, 1, 1, 0)\n"
        "    _cli._default_backend = _unexpected\n"
        "    _cli._custody_preflight = _unexpected\n"
        "    _cli.install_engine = _unexpected\n"
        "    _cli._check_install_identity = _unexpected\n"
        "    raise SystemExit(entries[0].load()())",
    )
    result = run(b, env, loader)
    assert result.returncode == 1
    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    required_bytes = sum(model.bytes for model in load_recipe_model_lock('comfyui', 'linux-x86_64-cu130').models)
    assert required_bytes > 0
    assert json.loads(result.stderr) == {'error': {
        'code': 'ENGINE_INSTALL_INSUFFICIENT_DISK', 'step': None,
        'retryable': True, 'recovery_handle': None,
        'message': 'Free disk space or choose a different --root, then retry.',
        'required_bytes': required_bytes, 'free_bytes': 0,
    }}
    assert not sentinel.exists()
    assert not root.exists()


def test_loader_spec_and_archive_boundaries():
    b = bootstrap.build_cli_bootstrap(ARGV)
    env = dict(b.env)
    env['ASTRID_ENGINE_SPEC'] = base64.b64encode(b'x' * 16385).decode()
    assert run(b, env).returncode != 0
    env = dict(b.env)
    env['ASTRID_ENGINE_SOURCE'] = base64.b64encode(lzma.compress(b'x' * 524289)).decode()
    assert run(b, env).returncode != 0


@pytest.mark.parametrize('form', ['egg-info', 'dist-info'])
def test_real_path_distribution_metadata_forms(monkeypatch, tmp_path, form):
    import importlib.metadata

    original = importlib.metadata.distribution('astrid')
    original_root = Path(original._path)
    metadata_file = 'PKG-INFO' if original_root.name.endswith('.egg-info') else 'METADATA'
    metadata = (original_root / metadata_file).read_bytes()
    entries = (original_root / 'entry_points.txt').read_bytes()
    root = tmp_path / ('astrid.' + form if form == 'egg-info' else 'astrid-0.1.0.dist-info')
    root.mkdir()
    (root / ('PKG-INFO' if form == 'egg-info' else 'METADATA')).write_bytes(metadata)
    (root / 'entry_points.txt').write_bytes(entries)
    dist = importlib.metadata.PathDistribution(root)
    monkeypatch.setattr(bootstrap.importlib.metadata, 'distributions', lambda **kwargs: [dist])
    bundle = bootstrap.build_cli_bootstrap(ARGV)
    with zipfile.ZipFile(io.BytesIO(unzip(bundle))) as archive:
        assert archive.read(f'astrid-{dist.version}.dist-info/METADATA') == metadata
        assert archive.read(f'astrid-{dist.version}.dist-info/entry_points.txt') == entries
    result = run(bundle)
    assert result.returncode == 0, result.stderr
    assert {s['kind'] for s in json.loads(result.stdout)['plan']['steps']} == {'source', 'environment', 'start', 'health'}


def test_missing_or_unsupported_distribution_metadata(monkeypatch, tmp_path):
    import importlib.metadata

    for dirname in ('astrid.egg-info', 'astrid-0.1.0.dist-info', 'astrid.other'):
        root = tmp_path / dirname
        root.mkdir()
        monkeypatch.setattr(bootstrap.importlib.metadata, 'distributions',
                            lambda **kwargs: [importlib.metadata.PathDistribution(root)])
        with pytest.raises(IntegrityError):
            bootstrap.build_cli_bootstrap(ARGV)
    root = tmp_path / 'partial.egg-info'
    root.mkdir()
    (root / 'PKG-INFO').write_bytes(b'Metadata-Version: 2.1\nName: astrid\nVersion: 0.1.0\n')
    monkeypatch.setattr(bootstrap.importlib.metadata, 'distributions',
                        lambda **kwargs: [importlib.metadata.PathDistribution(root)])
    with pytest.raises(IntegrityError):
        bootstrap.build_cli_bootstrap(ARGV)
    monkeypatch.setattr(bootstrap.importlib.metadata, 'distributions', lambda **kwargs: [object()])
    with pytest.raises(IntegrityError):
        bootstrap.build_cli_bootstrap(ARGV)


def test_stale_or_missing_local_metadata_refused(monkeypatch, tmp_path):
    import importlib.metadata
    import shutil

    root = Path(bootstrap.__file__).resolve().parents[3]
    for name in bootstrap._FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    fake = tmp_path / 'astrid/core/generation/engine_bootstrap.py'
    fake.write_text('')
    shutil.copyfile(root / 'astrid/core/generation/_engine_bootstrap_loader.py',
                    fake.parent / '_engine_bootstrap_loader.py')
    shutil.copyfile(root / 'pyproject.toml', tmp_path / 'pyproject.toml')
    monkeypatch.setattr(bootstrap, '__file__', str(fake))
    with pytest.raises(IntegrityError):
        bootstrap.build_cli_bootstrap(ARGV)
    shutil.copytree(root / 'astrid.egg-info', tmp_path / 'astrid.egg-info')
    entries = tmp_path / 'astrid.egg-info/entry_points.txt'
    entries.write_text(entries.read_text().replace('astrid.console:main', 'astrid.omp_agent:main'))
    with pytest.raises(IntegrityError):
        bootstrap.build_cli_bootstrap(ARGV)
    shutil.copyfile(root / 'astrid.egg-info/entry_points.txt', entries)
    metadata = tmp_path / 'astrid.egg-info/PKG-INFO'
    metadata.write_text(metadata.read_text().replace('Version: 0.1.0', 'Version: 9.9.9'))
    with pytest.raises(IntegrityError):
        bootstrap.build_cli_bootstrap(ARGV)


def test_implicit_local_root_planning_in_bootstrap(tmp_path):
    from astrid.core.generation.engine_local_endpoint import record_engine_root

    root = str(tmp_path / 'registered')
    record_engine_root('comfyui', root)
    bundle = bootstrap.build_cli_bootstrap(('engines', 'install', 'comfyui', '--dry-run'))
    result = run(bundle, env={**bundle.env, 'HOME': str(Path.home())})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['plan']['root'] == root
