"""Verified release pre-CLI managed-interpreter handoff.

Synthetic release trees below are BOOTSTRAP-PROCESS FIXTURES. Their manifests
claim ``complete=true`` only to reach the handoff gate; they carry the real
verifier, launcher, shared bootstrap helper and (where stated) the real H3
recipe, plus a tiny synthetic ``astrid`` console. They are never a complete
artifact, dependency closure, engine installation or GPU qualification.
Fake ``python3``/``uv`` tools record argv and create a venv whose interpreter
is a REAL installed CPython, so identity checks always observe a live process.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from astrid.core.generation import _engine_python_bootstrap as shared
from astrid.core.generation import engine_release as release
from astrid.core.generation.engine_platforms import platform_adapter
from astrid.core.generation.engine_recipe import load_recipe

REPO = Path(__file__).resolve().parents[3]
GENERATION = REPO / 'astrid/core/generation'
RECIPE = REPO / 'astrid/packs/runpod/recipe.linux-cu130.json'
KEY = 'linux-x86_64-cu130'
CURRENT = os.path.realpath(sys.executable)
CURRENT_VERSION = list(sys.version_info[:3])
LOADER = (GENERATION / 'engine_release.py').read_text(encoding='utf-8')
REAL_OWNED = REPO / '.release-python-real'


def _identity(version):
    return {'implementation': 'cpython', 'version': list(version), 'platform': 'linux', 'machine': 'x86_64'}


def _version(python):
    out = subprocess.run([python, '-I', '-S', '-c', 'import sys;print(*sys.version_info[:3])'],
                         capture_output=True, text=True, timeout=30)
    return [int(p) for p in out.stdout.split()] if out.returncode == 0 else None


def _other_system_python():
    for candidate in ('/usr/bin/python3.12', '/usr/bin/python3.11', '/usr/bin/python3.13', '/usr/bin/python3'):
        if os.path.exists(candidate) and _version(candidate) not in (None, CURRENT_VERSION):
            return candidate
    return None


OTHER = _other_system_python()
needs_other = pytest.mark.skipif(OTHER is None, reason='no second system CPython patch version installed')

CONSOLE = b'''import json, sys
def main(argv):
    print(json.dumps({"argv": list(argv), "executable": sys.executable,
                      "version": list(sys.version_info[:3])}))
    return 0
'''
STARTUP = b'''import json, sys
def main(argv, *, release_root):
    print(json.dumps({"startup": list(argv), "executable": sys.executable,
                      "version": list(sys.version_info[:3]), "release": str(release_root)}))
    return 0
'''
# Any product import records the importing interpreter version.
PRODUCT_INIT = b'''import json, os, sys
_marker = os.environ.get("RELEASE_PYTHON_IMPORT_MARKER")
if _marker:
    with open(_marker, "a") as _stream:
        _stream.write(json.dumps(list(sys.version_info[:3])) + "\\n")
'''


def _recipe_bytes(python_version=None, uv_version=None):
    raw = RECIPE.read_bytes()
    if python_version is None and uv_version is None:
        return raw
    text = raw.decode()
    if python_version is not None:
        text = text.replace('"python_version": "3.12.10"', f'"python_version": "{python_version}"')
    if uv_version is not None:
        text = text.replace('"uv_version": "0.11.32"', f'"uv_version": "{uv_version}"')
    return text.encode()


def make_release(work, *, python, recipe=None, complete=True, index_sha=None):
    """Synthetic bootstrap-process fixture archive; see module docstring."""
    recipe = _recipe_bytes() if recipe is None else recipe
    index = {'schema': 'astrid.engine-index.v1', 'entries': [{
        'engine': 'vibecomfy-h3', 'platform': KEY,
        'recipe': {'path': 'packs/runpod/recipe.linux-cu130.json',
                   'sha256': index_sha or hashlib.sha256(recipe).hexdigest()}}]}
    data = {
        'run.py': (GENERATION / '_engine_release_entry.py').read_bytes(),
        'site/astrid/core/generation/engine_release.py': (GENERATION / 'engine_release.py').read_bytes(),
        'site/astrid/core/generation/_engine_python_bootstrap.py':
            (GENERATION / '_engine_python_bootstrap.py').read_bytes(),
        'site/astrid/core/generation/engine_startup.py': STARTUP,
        'site/astrid/__init__.py': PRODUCT_INIT,
        'site/astrid/console.py': CONSOLE,
        'site/astrid/packs/runpod/recipe.linux-cu130.json': recipe,
        'site/astrid/packs/engine-index.json': json.dumps(index).encode(),
        'site/astrid-0.0.0.dist-info/METADATA': b'Metadata-Version: 2.1\nName: astrid\nVersion: 0.0.0\n',
        'site/astrid-0.0.0.dist-info/entry_points.txt': b'[console_scripts]\nastrid = astrid.console:main\n',
    }
    manifest = {'format': 1, 'complete': complete, 'fixture': not complete, 'python': python,
                'missing': [] if complete else ['synthetic-fixture'], 'sources': {}, 'wheels': [],
                'files': {n: {'sha256': hashlib.sha256(b).hexdigest(), 'bytes': len(b)} for n, b in data.items()}}
    data['release.json'] = release.canonical(manifest)
    work.mkdir(parents=True, exist_ok=True)
    archive = work / 'release.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        for name, content in sorted(data.items()):
            item = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = (0o100444) << 16
            bundle.writestr(item, content)
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


FAKE_PYTHON3 = '''#!{python} -I
import json, os, pathlib, sys, time
config = json.loads(pathlib.Path({config!r}).read_text())
with open(config["log"], "a") as log:
    log.write(json.dumps({{"argv": ["python3", *sys.argv[1:]], "env": dict(os.environ)}}) + "\\n")
mode = config.get("pip", "ok")
if sys.argv[1:] == ["-m", "pip", "--version"]:
    raise SystemExit(1 if mode == "missing" else 0)
if mode == "hang":
    time.sleep(60)
prefix = pathlib.Path(sys.argv[sys.argv.index("--prefix") + 1])
(prefix / "bin").mkdir(parents=True, exist_ok=True)
uv = prefix / "bin/uv"
uv.write_text(pathlib.Path({uv_source!r}).read_text())
uv.chmod(0o755)
'''
FAKE_UV = '''#!{python} -I
import json, os, pathlib, sys
config = json.loads(pathlib.Path({config!r}).read_text())
with open(config["log"], "a") as log:
    log.write(json.dumps({{"argv": [sys.argv[0], *sys.argv[1:]], "env": dict(os.environ)}}) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("uv " + config.get("uv_version", "0.11.32") + " (fake)")
    raise SystemExit(0)
venv = pathlib.Path(sys.argv[-1])
(venv / "bin").mkdir(parents=True, exist_ok=True)
if not (venv / "bin/python").exists():
    (venv / "bin/python").symlink_to(config["managed_python"])
'''


class Tools:
    """Fake bootstrap tools. Their configuration is a file, never inherited env,
    because bootstrap children receive only an allowlisted environment."""

    def __init__(self, tmp_path):
        self.bin = tmp_path / 'fake-bin'
        self.bin.mkdir()
        self.config_path = tmp_path / 'fake-config.json'
        self.log = tmp_path / 'bootstrap.jsonl'
        self.marker = tmp_path / 'imports.jsonl'
        uv_source = tmp_path / 'fake-uv.py'
        uv_source.write_text(FAKE_UV.format(python=CURRENT, config=str(self.config_path)))
        (self.bin / 'python3').write_text(FAKE_PYTHON3.format(python=CURRENT, config=str(self.config_path),
                                                              uv_source=str(uv_source)))
        (self.bin / 'python3').chmod(0o755)
        self.configure()
        self.env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': str(tmp_path), 'LANG': 'C.UTF-8',
                    'RELEASE_PYTHON_IMPORT_MARKER': str(self.marker), 'ASTRID_POD_TOKEN': 'x' * 40}

    def configure(self, **values):
        self.config_path.write_text(json.dumps({'log': str(self.log), 'managed_python': CURRENT, **values}))

    def entries(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def imports(self):
        return [json.loads(line) for line in self.marker.read_text().splitlines()] if self.marker.exists() else []


@pytest.fixture
def tools(tmp_path):
    return Tools(tmp_path)


def _loader(python, operation, archive, sha, retain, root, *extra, env, seconds=60):
    return subprocess.run([python, '-I', '-S', '-c', LOADER, operation, '--archive', str(archive),
                           '--sha256', sha, '--retain', str(retain), '--root', str(root),
                           '--seconds', str(seconds), *extra],
                          env=env, capture_output=True, text=True, timeout=120)


REFUSED = 'release verification/build failed'
CLI = ('engines', 'install', 'vibecomfy-h3', '--placement', 'local', '--platform', KEY, '--dry-run')


# ---- shared argv: one construction, byte-equivalent to the adapter -------------

def test_shared_helper_is_adapter_argv_and_environment_for_real_recipe():
    recipe = load_recipe('vibecomfy-h3', KEY)
    primary = next(e for e in recipe.environments if e.name == recipe.primary_environment)
    adapter = platform_adapter(KEY)
    root = '/workspace/astrid/engines/vibecomfy-h3'
    helper = shared.linux_bootstrap_commands(install_root=root, uv_version=primary.uv_version,
                                             python_version=primary.python_version,
                                             venv_relative_path=primary.relative_path)
    assert adapter.environment_commands(recipe, primary, root) == helper
    # Literal regression of the pre-refactor adapter argv.
    assert helper == (
        ('python3', '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple',
         '--ignore-installed', '--prefix', root + '/runtime/bootstrap', 'uv==0.11.32'),
        (root + '/runtime/bootstrap/bin/uv', '--no-config', 'venv', '--allow-existing', '--python',
         '3.12.10', '--managed-python', root + '/runtime/venv'))
    assert adapter.environment(recipe, root) == shared.linux_bootstrap_environment(install_root=root)
    paths = shared.linux_bootstrap_paths(install_root=root, venv_relative_path=primary.relative_path)
    assert paths['uv'] == adapter.uv_executable(root + '/runtime')
    assert paths['python'] == adapter.venv_python(root + '/' + primary.relative_path)


def test_shared_helper_is_stdlib_only_effect_free_and_strict():
    tree = ast.parse((GENERATION / '_engine_python_bootstrap.py').read_text())
    imported = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported <= {'__future__', 're'}
    good = dict(install_root='/r', uv_version='0.11.32', python_version='3.12.10', venv_relative_path='runtime/venv')
    for change in ({'install_root': 'relative'}, {'install_root': '/a/../b'}, {'install_root': '/'},
                   {'install_root': '/a/'}, {'install_root': '//a'}, {'install_root': '/a:b'},
                   {'venv_relative_path': '../venv'}, {'venv_relative_path': '/abs'},
                   {'uv_version': '0.11'}, {'uv_version': '>=0.11.32'}, {'python_version': '3.12'},
                   {'python_version': '3.12.10; rm'}):
        with pytest.raises(ValueError):
            shared.linux_bootstrap_commands(**{**good, **change})


# ---- matching interpreter: direct, no bootstrap ------------------------------------

def test_matching_interpreter_execs_retained_cli_directly(tmp_path, tools):
    env, entries, imports = tools.env, tools.entries, tools.imports
    archive, sha = make_release(tmp_path / 'r', python=_identity(CURRENT_VERSION),
                                recipe=_recipe_bytes('.'.join(map(str, CURRENT_VERSION))))
    root = tmp_path / 'engine'
    located = _loader(CURRENT, 'prepare-python', archive, sha, tmp_path / 'keep', root, env=env)
    assert located.returncode == 0, located.stderr
    locator = json.loads(located.stdout)
    assert locator == {'schema': 'astrid.release-python.v1', 'python': CURRENT, 'bootstrapped': False,
                       'release': str(tmp_path / 'keep' / ('sha256-' + sha)),
                       'run': str(tmp_path / 'keep' / ('sha256-' + sha) / 'run.py'),
                       'identity': _identity(CURRENT_VERSION)}
    result = _loader(CURRENT, 'cli', archive, sha, tmp_path / 'keep', root, '--',
                     *CLI, '--root', str(root), env=env)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out['argv'] == [*CLI, '--root', str(root)] and out['version'] == CURRENT_VERSION
    assert entries() == [] and not root.exists()
    assert imports() == [CURRENT_VERSION]


# ---- mismatched system interpreter: bootstrap, verify, exec --------------------

@needs_other
def test_mismatched_system_python_bootstraps_then_execs_same_release(tmp_path, tools):
    env, entries, imports = tools.env, tools.entries, tools.imports
    pin = '.'.join(map(str, CURRENT_VERSION))
    archive, sha = make_release(tmp_path / 'r', python=_identity(CURRENT_VERSION), recipe=_recipe_bytes(pin))
    root = tmp_path / 'engine'
    result = _loader(OTHER, 'cli', archive, sha, tmp_path / 'keep', root, '--',
                     *CLI, '--root', str(root), env=env)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    venv_python = str(root / 'runtime/venv/bin/python')
    assert out['executable'] == venv_python and out['version'] == CURRENT_VERSION
    assert out['argv'] == [*CLI, '--root', str(root)]
    # Exactly the shared helper's commands, in order, around pip/uv/identity checks.
    commands = shared.linux_bootstrap_commands(install_root=str(root), uv_version='0.11.32',
                                               python_version=pin, venv_relative_path='runtime/venv')
    argvs = [tuple(e['argv']) for e in entries()]
    assert argvs == [('python3', '-m', 'pip', '--version'), commands[0],
                     (str(root / 'runtime/bootstrap/bin/uv'), '--version'), commands[1]]
    overlay = {'PYTHONDONTWRITEBYTECODE': '1', 'UV_CACHE_DIR': str(root / 'runtime/uv-cache')}
    public = {'PATH': env['PATH'], 'HOME': env['HOME'], 'LANG': 'C.UTF-8'}
    # The Debian layout variable is scoped to the pip --prefix command only.
    assert [e['env'] for e in entries()] == [
        {**public, **overlay}, {**public, **overlay, 'DEB_PYTHON_INSTALL_LAYOUT': 'deb'},
        {**public, **overlay}, {**public, **overlay}]
    # Product code was imported only by the compatible managed interpreter.
    assert imports() == [CURRENT_VERSION]
    # The exec'd launcher still enforces its exact manifest identity.
    os.chmod(root / 'runtime/venv/bin', 0o755)
    (root / 'runtime/venv/bin/python').unlink()
    (root / 'runtime/venv/bin/python').symlink_to(OTHER)
    refused = _loader(OTHER, 'cli', archive, sha, tmp_path / 'keep', root, '--',
                      *CLI, '--root', str(root), env=env)
    assert refused.returncode == 1 and refused.stderr.strip() == REFUSED
    assert imports() == [CURRENT_VERSION]


@needs_other
def test_release_cli_argv_is_the_single_job_seam(tmp_path, tools):
    env, entries, imports = tools.env, tools.entries, tools.imports
    pin = '.'.join(map(str, CURRENT_VERSION))
    archive, sha = make_release(tmp_path / 'r', python=_identity(CURRENT_VERSION), recipe=_recipe_bytes(pin))
    root = str(tmp_path / 'engine')
    argv = release.release_cli_argv(archive=str(archive), sha256=sha, retain=str(tmp_path / 'keep'),
                                    root=root, cli_args=(*CLI, '--root', root), seconds=90, python=OTHER)
    assert argv[:4] == (OTHER, '-I', '-S', '-c') and argv[5] == 'cli'
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['executable'] == root + '/runtime/venv/bin/python'
    for bad in ((*CLI,), (*CLI, '--root', root + '-other'), (*CLI, '--root', root, '--root', root)):
        with pytest.raises(release.ReleaseError, match='root'):
            release.release_cli_argv(archive=str(archive), sha256=sha, retain=str(tmp_path / 'keep'),
                                     root=root, cli_args=bad)
    refused = _loader(OTHER, 'cli', archive, sha, tmp_path / 'keep', root, '--', *CLI, env=env)
    assert refused.returncode == 1 and refused.stderr.strip() == REFUSED


@needs_other
def test_selfstart_hands_off_and_charges_budget(tmp_path, tools):
    env, entries, imports = tools.env, tools.entries, tools.imports
    pin = '.'.join(map(str, CURRENT_VERSION))
    archive, sha = make_release(tmp_path / 'r', python=_identity(CURRENT_VERSION), recipe=_recipe_bytes(pin))
    root = tmp_path / 'engine'
    result = subprocess.run([OTHER, '-I', '-S', '-c', LOADER, 'start', '--archive', str(archive),
                             '--sha256', sha, '--retain', str(tmp_path / 'keep'), '--volume', str(tmp_path),
                             '--root', str(root), '--attempt', 'A', '--seconds', '120',
                             '--step-seconds', '600'], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out['executable'] == str(root / 'runtime/venv/bin/python')
    args = out['startup']
    assert args[:6] == ['--volume', str(tmp_path), '--root', str(root), '--attempt', 'A']
    seconds = float(args[args.index('--seconds') + 1])
    assert 0 < seconds < 120 and float(args[args.index('--step-seconds') + 1]) == seconds


SEEDED = {
    'RUNPOD_API_KEY': 'rpa_seeded-provider-key', 'ASTRID_POD_TOKEN': 'p' * 40,
    'ASTRID_POD_AGENT_B64': 'c2VlZGVk', 'ASTRID_POD_OBSERVATION_B64': 'c2VlZGVk',
    'ASTRID_HOST_BRIDGE_TOKEN': 'seeded-bridge', 'ASTRID_ACTION_BRIDGE_TOKEN': 'seeded-bridge-2',
    'ASTRID_RUNTIME_OWNER_TOKEN': 'seeded-owner', 'SSH_AUTH_SOCK': '/tmp/seeded-agent.sock',
    'SSH_PRIVATE_KEY': 'seeded-ssh-key', 'AWS_SECRET_ACCESS_KEY': 'seeded-aws',
    'UV_INDEX_URL': 'https://seeded.invalid/simple', 'UV_PYTHON_INSTALL_DIR': '/seeded/uv-python',
    'UV_CACHE_DIR': '/seeded/uv-cache', 'UV_NATIVE_TLS': '1', 'PYTHONPATH': '/seeded/pythonpath',
    'PYTHONSTARTUP': '/seeded/startup.py', 'PYTHONHOME': '/seeded/home', 'PIP_INDEX_URL': 'https://seeded.invalid',
}
ALLOWED = set(release._CHILD_ENV) | {'PYTHONDONTWRITEBYTECODE', 'UV_CACHE_DIR', 'DEB_PYTHON_INSTALL_LAYOUT'}


@needs_other
def test_bootstrap_children_receive_only_allowlisted_environment(tmp_path, tools):
    env, entries, imports = tools.env, tools.entries, tools.imports
    pin = '.'.join(map(str, CURRENT_VERSION))
    archive, sha = make_release(tmp_path / 'r', python=_identity(CURRENT_VERSION), recipe=_recipe_bytes(pin))
    root = tmp_path / 'engine'
    seeded = dict(env, **SEEDED, HTTPS_PROXY='http://proxy.invalid:3128', SSL_CERT_FILE='/etc/ssl/certs/ca.pem')
    # PYTHONHOME would break the real loader interpreter itself; -I ignores the
    # PYTHON* variables for it, so only children could be affected.
    result = _loader(OTHER, 'cli', archive, sha, tmp_path / 'keep', root, '--',
                     *CLI, '--root', str(root), env=seeded)
    assert result.returncode == 0, result.stderr
    children = entries()
    assert len(children) == 4
    for child in children:
        assert set(child['env']) <= ALLOWED, set(child['env']) - ALLOWED
        assert not set(child['env']) & (set(SEEDED) - {'UV_CACHE_DIR'})
        assert child['env']['UV_CACHE_DIR'] == str(root / 'runtime/uv-cache')
        assert child['env']['HTTPS_PROXY'] == 'http://proxy.invalid:3128'
        assert child['env']['SSL_CERT_FILE'] == '/etc/ssl/certs/ca.pem'
        assert not any(v in json.dumps(child['env']) for v in SEEDED.values() if v != '1')
    assert 'DEB_PYTHON_INSTALL_LAYOUT' in children[1]['env']
    assert all('DEB_PYTHON_INSTALL_LAYOUT' not in c['env'] for i, c in enumerate(children) if i != 1)
    # The exec'd canonical CLI itself is the original job's process: unchanged env.
    assert json.loads(result.stdout)['executable'] == str(root / 'runtime/venv/bin/python')


# ---- the real recipe binds 3.12.10/Linux x86_64 --------------------------------

def test_real_recipe_expected_identity_is_cpython_3_12_10_linux_x86_64(tmp_path):
    archive, sha = make_release(tmp_path / 'r', python=_identity([3, 12, 10]))
    retained = release.extract_release(archive, sha, tmp_path / 'keep')
    environment = release._handoff_recipe(retained)
    assert environment == {'relative_path': 'runtime/venv', 'python_version': '3.12.10', 'uv_version': '0.11.32'}
    assert release.expected_identity(environment['python_version']) == _identity([3, 12, 10])


@needs_other
def test_managed_interpreter_must_be_the_recipe_version(tmp_path, tools):
    env, entries, imports = tools.env, tools.entries, tools.imports
    # Real recipe (3.12.10); the venv interpreter is really a different CPython.
    archive, sha = make_release(tmp_path / 'r', python=_identity([3, 12, 10]))
    root = tmp_path / 'engine'
    result = _loader(OTHER, 'cli', archive, sha, tmp_path / 'keep', root, '--',
                     *CLI, '--root', str(root), env=env)
    assert result.returncode == 1 and result.stderr.strip() == REFUSED
    assert [e['argv'][-1] for e in entries()][-1] == str(root / 'runtime/venv')
    assert imports() == []


# ---- refusals before any bootstrap ---------------------------------------------

@pytest.mark.parametrize('case', ['altered-archive', 'incomplete-fixture', 'manifest-not-recipe',
                                  'recipe-index-digest', 'inexact-recipe-pin', 'bad-root', 'tampered-tree'])
def test_refusal_before_bootstrap_or_product_import(tmp_path, tools, case):
    env, entries, imports = tools.env, tools.entries, tools.imports
    python, recipe, complete, index_sha = _identity([3, 12, 10]), None, True, None
    if case == 'incomplete-fixture':
        complete = False
    elif case == 'manifest-not-recipe':
        python = _identity([3, 12, 9])
    elif case == 'recipe-index-digest':
        index_sha = '0' * 64
    elif case == 'inexact-recipe-pin':
        recipe = _recipe_bytes('3.12')
        python = _identity([3, 12, 0])
    archive, sha = make_release(tmp_path / 'r', python=python, recipe=recipe, complete=complete,
                                index_sha=index_sha)
    root = tmp_path / 'engine'
    if case == 'altered-archive':
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 1
        archive.write_bytes(data)
    if case == 'bad-root':
        root = str(tmp_path) + '/x/../engine'
    if case == 'tampered-tree':
        retained = release.extract_release(archive, sha, tmp_path / 'keep')
        target = retained / 'site/astrid/core/generation/_engine_python_bootstrap.py'
        target.chmod(0o600)
        target.write_bytes(target.read_bytes() + b'\n# tampered\n')
    python_bin = OTHER or CURRENT
    for operation in ('prepare-python', 'cli'):
        extra = ('--', *CLI, '--root', str(root)) if operation == 'cli' else ()
        result = _loader(python_bin, operation, archive, sha, tmp_path / 'keep', root, *extra, env=env)
        assert result.returncode == 1 and result.stderr.strip() == REFUSED, case
    assert entries() == [] and imports() == []
    assert not Path(str(root)).exists() and not (tmp_path / 'engine').exists()


def test_wrong_platform_refused_before_bootstrap(tmp_path, tools, monkeypatch):
    env, entries, imports = tools.env, tools.entries, tools.imports
    archive, sha = make_release(tmp_path / 'r', python=_identity([3, 12, 10]))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(release, 'interpreter_identity', lambda: {**_identity([3, 12, 10]), 'machine': 'aarch64'})
    with pytest.raises(release.ReleaseError, match='platform'):
        release.prepare_release_python(archive=archive, sha256=sha, retain=tmp_path / 'keep',
                                       root=str(tmp_path / 'engine'), seconds=30)
    assert entries() == [] and not (tmp_path / 'engine').exists()
    with pytest.raises(release.ReleaseError):
        release.expected_identity('3.12.10', 'windows-x86_64-cu130')


# ---- refused bootstrap steps fail closed ---------------------------------------

@pytest.mark.parametrize('case', ['missing-pip', 'wrong-uv-pin', 'bounded-hang'])
def test_refused_bootstrap_fails_closed(tmp_path, tools, monkeypatch, case):
    env, entries, imports = tools.env, tools.entries, tools.imports
    pin = '.'.join(map(str, CURRENT_VERSION))
    archive, sha = make_release(tmp_path / 'r', python=_identity(CURRENT_VERSION), recipe=_recipe_bytes(pin))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    tools.configure(pip={'missing-pip': 'missing', 'bounded-hang': 'hang'}.get(case, 'ok'),
                    uv_version='0.11.31' if case == 'wrong-uv-pin' else '0.11.32')
    # The running test interpreter must differ from the expected identity.
    monkeypatch.setattr(release, 'interpreter_identity', lambda: _identity([0, 0, 1]))
    root = tmp_path / 'engine'
    started = time.monotonic()
    with pytest.raises(release.ReleaseError) as refused:
        release.prepare_release_python(archive=archive, sha256=sha, retain=tmp_path / 'keep',
                                       root=str(root), seconds=3 if case == 'bounded-hang' else 60)
    argvs = [e['argv'] for e in entries()]
    if case == 'missing-pip':
        assert 'pip' in str(refused.value) and argvs == [['python3', '-m', 'pip', '--version']]
        assert not root.exists()
    elif case == 'wrong-uv-pin':
        assert 'uv' in str(refused.value) and argvs[-1][-1] == '--version'
        assert not (root / 'runtime/venv').exists()
    else:
        assert 'bound' in str(refused.value) and time.monotonic() - started < 15
        assert len(argvs) == 2 and not (root / 'runtime/bootstrap').exists()
    assert imports() == []


# ---- real recipe bootstrap and real managed CPython 3.12.10 identity ----------

def _real_enabled():
    flag = os.environ.get('ASTRID_RELEASE_PYTHON_REAL')
    if flag is not None:
        return flag == '1'
    return any((REAL_OWNED / 'home/.local/share/uv/python').glob('cpython-3.12.10-linux-x86_64-*'))


@pytest.mark.skipif(not _real_enabled(), reason='owned managed CPython 3.12.10 install not prepared (set ASTRID_RELEASE_PYTHON_REAL=1)')
def test_real_recipe_bootstrap_reaches_managed_cpython_3_12_10(tmp_path):
    """Real system python3+pip, real pinned uv 0.11.32 and real managed CPython.

    Uses the REAL recipe and its exact shared commands. Network: PyPI for the
    pinned uv wheel (and, once, the managed CPython). uv's managed install
    directory lives under an owned HOME in this checkout; no shared venv/cache
    is mutated. No dependency, source,
    model or engine is installed.
    """
    archive, sha = make_release(tmp_path / 'r', python=_identity([3, 12, 10]))
    root = tmp_path / 'engine'
    marker = tmp_path / 'imports.jsonl'
    # Owned persistent HOME under this checkout: uv's default managed-Python
    # directory ($HOME/.local/share/uv/python) is then owned, not shared.
    (REAL_OWNED / 'home').mkdir(parents=True, exist_ok=True)
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(REAL_OWNED / 'home'), 'LANG': 'C.UTF-8',
           'RELEASE_PYTHON_IMPORT_MARKER': str(marker), 'ASTRID_POD_TOKEN': 'x' * 40}
    for name in ('HTTPS_PROXY', 'https_proxy', 'NO_PROXY', 'no_proxy', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'):
        if name in os.environ:
            env[name] = os.environ[name]
    system = subprocess.run(['python3', '-I', '-S', '-c', 'import sys;print(*sys.version_info[:3])'],
                            env=env, capture_output=True, text=True, timeout=30)
    assert system.stdout.split() != ['3', '12', '10'], 'system python3 must differ for this test'
    result = _loader('/usr/bin/python3', 'cli', archive, sha, tmp_path / 'keep', root, '--',
                     *CLI, '--root', str(root), env=env, seconds=600)
    assert result.returncode == 0, result.stderr[-2000:]
    out = json.loads(result.stdout.splitlines()[-1])
    assert out['executable'] == str(root / 'runtime/venv/bin/python') and out['version'] == [3, 12, 10]
    assert (root / 'runtime/bootstrap/bin/uv').is_file()
    assert [json.loads(line) for line in marker.read_text().splitlines()] == [[3, 12, 10]]
    probe = subprocess.run([str(root / 'runtime/venv/bin/python'), '-I', '-S', '-c', release._IDENTITY_PROBE],
                           capture_output=True, text=True, timeout=30)
    assert json.loads(probe.stdout) == _identity([3, 12, 10])
