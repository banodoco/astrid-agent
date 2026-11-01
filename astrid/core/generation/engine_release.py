"""Whole-wheel release archives for the canonical on-target engine CLI.

This module also runs as an isolated stdlib verification/extraction program.
It never fetches, publishes, resolves dependencies, or installs an engine.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

MAX_ARCHIVE = 1024 * 1024 * 1024
MAX_FILES = 50000
MAX_MANIFEST = 16 * 1024 * 1024
PRODUCTS = ('astrid', 'astrid-runtime', 'runpod-lifecycle', 'runpod')
REQUIRED = (
    'astrid/console.py', 'astrid/core/engine_cli.py',
    'astrid/core/generation/engine_release.py',
    'astrid/core/generation/_engine_release_entry.py',
    'astrid/core/generation/_engine_python_bootstrap.py',
    'astrid/core/generation/engine_startup.py',
    'astrid/core/generation/engine_readiness.py',
    'astrid/core/generation/engine_execution.py',
    'astrid/core/generation/_engine_target_files.py',
    'astrid/core/generation/engine_custody.py',
    'astrid/core/generation/engine_runner_target.py',
    'runtime_protocol/platform.py', 'runtime_protocol/cancellation.py',
    'runpod_lifecycle/pod_agent.py',
)


# The one engine/platform whose recipe-owned managed interpreter a release
# may hand off to. Expected identity always comes from the verified recipe.
HANDOFF_ENGINE = 'vibecomfy-h3'
HANDOFF_PLATFORM = 'linux-x86_64-cu130'
HANDOFF_SCHEMA = 'astrid.release-python.v1'
# Bootstrap children get ONLY these inherited public variables plus the
# recipe overlay: no agent/owner/bridge tokens, provider keys, SSH material,
# or inherited UV_*/PYTHON* configuration.
_CHILD_ENV = ('PATH', 'HOME', 'LANG', 'LC_ALL',
              'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'NO_PROXY', 'no_proxy',
              'ALL_PROXY', 'all_proxy',
              'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE')
_IDENTITY_PROBE = ('import json,platform,sys;print(json.dumps({"implementation":sys.implementation.name,'
                   '"version":list(sys.version_info[:3]),"platform":sys.platform,'
                   '"machine":platform.machine()}))')


class ReleaseError(ValueError):
    """Sanitized release validation failure, before product execution."""


def interpreter_identity():
    return {'implementation': sys.implementation.name, 'version': list(sys.version_info[:3]),
            'platform': sys.platform, 'machine': platform.machine()}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _hash_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReleaseError('duplicate JSON field')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ReleaseError('invalid release JSON') from None


def _relative(name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(ord(c) < 32 for c in name) or PurePosixPath(name).is_absolute()
            or any(p in ('', '.', '..') for p in name.split('/'))):
        raise ReleaseError('unsafe archive path')
    return name


def _regular(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ReleaseError('symlink in release path')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ReleaseError('release input is not a singly linked regular file')
    return info


def _provenance(value, wheels):
    # Trusted build inputs, checked structurally and bound to exact wheel bytes.
    if not isinstance(value, dict) or set(value) != set(PRODUCTS):
        raise ReleaseError('all four frozen source records are required')
    for name, record in value.items():
        if (not isinstance(record, dict) or set(record) != {'commit', 'patches', 'wheel_sha256'}
                or not re.fullmatch('[0-9a-f]{40}', record.get('commit', ''))
                or not isinstance(record['patches'], list)):
            raise ReleaseError('invalid frozen source record')
        for patch in record['patches']:
            if (not isinstance(patch, dict) or set(patch) != {'name', 'sha256'}
                    or not re.fullmatch('[A-Za-z0-9_.-]+', patch.get('name', ''))
                    or not re.fullmatch('[0-9a-f]{64}', patch.get('sha256', ''))):
                raise ReleaseError('invalid additional patch inventory')
        if record['wheel_sha256'] != wheels.get(name):
            raise ReleaseError('source provenance does not bind the product wheel')


def _inventory(site):
    result = {}
    total = 0
    for path in sorted(site.rglob('*')):
        if path.is_symlink():
            raise ReleaseError('installed wheel contains a symlink')
        if path.is_dir():
            continue
        _regular(path)
        total += path.stat().st_size
        if total > MAX_ARCHIVE or len(result) >= MAX_FILES:
            raise ReleaseError('installed wheel tree exceeds release limits')
        name = path.relative_to(site).as_posix()
        # No site hooks or ambient editable installs in a retained release.
        if path.suffix == '.pth' or path.name in ('sitecustomize.py', 'usercustomize.py'):
            raise ReleaseError('wheel contains a Python startup hook')
        result[name] = path.read_bytes()
    return result


def _audit(site):
    """Conservative metadata closure; no optional dependency is silently supplied."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.markers import default_environment

    missing = ['module:' + name for name in REQUIRED if not (site / name).is_file()]
    distributions = {}
    for dist in importlib.metadata.distributions(path=[str(site)]):
        name = canonicalize_name(dist.metadata['Name'])
        if name in distributions:
            raise ReleaseError('duplicate installed distribution')
        distributions[name] = dist
    for name in PRODUCTS:
        if name not in distributions:
            missing.append('distribution:' + name)
    environment = default_environment()
    environment.update(sys_platform='linux', platform_system='Linux', platform_machine='x86_64', extra='')
    extras = {name: {''} for name in distributions}
    changed = True
    while changed:
        changed = False
        for name, dist in sorted(distributions.items()):
            for text in dist.requires or ():
                req = Requirement(text)
                if req.marker is not None and not any(
                        req.marker.evaluate(dict(environment, extra=extra)) for extra in extras[name]):
                    continue
                dependency = canonicalize_name(req.name)
                installed = distributions.get(dependency)
                if req.url is not None:
                    # Direct references need an exact reviewed mapping, never ambient resolution.
                    missing.append('direct-dependency:' + name + ':' + str(req))
                elif installed is None or not req.specifier.contains(installed.version):
                    missing.append('dependency:' + name + ':' + str(req))
                if installed is not None and not req.extras.issubset(extras[dependency]):
                    extras[dependency].update(req.extras)
                    changed = True
    recipe_path = site / 'astrid/packs/runpod/recipe.linux-cu130.json'
    if not recipe_path.is_file():
        missing.append('recipe:linux-x86_64-cu130')
    else:
        recipe = _json(recipe_path.read_bytes())
        for env in recipe['environments']:
            req = env['requirements']
            if req['mode'] != 'packaged' or req['packaged_lock'] is None:
                missing.append('resolved-linux-lock:' + env['name'])
            else:
                ref = req['packaged_lock']
                path = site / 'astrid' / _relative(ref['path'])
                if not path.is_file() or _hash_file(path) != ref['sha256']:
                    missing.append('resolved-linux-lock:' + env['name'])
    return sorted(set(missing))


def build_release(wheels, provenance, output, *, fixture=False):
    """Install full wheels with ordinary offline pip, then archive exact files.

    ``fixture=True`` retains missing-input facts and is never launchable as a
    production startup. The default refuses missing locks/modules/dependencies.
    Source-to-wheel provenance is the trusted producer's assertion, bound to
    wheel hashes here; it is not a reproducible-build attestation.
    """
    wheels = tuple(Path(p).absolute() for p in wheels)
    if not wheels or len({p.name for p in wheels}) != len(wheels):
        raise ReleaseError('expected unique input wheels')
    wheel_rows, product_hashes = [], {}
    for path in wheels:
        _regular(path)
        if path.suffix != '.whl':
            raise ReleaseError('only ordinary wheels are supported')
        with zipfile.ZipFile(path) as wheel:
            metadata = [n for n in wheel.namelist() if n.endswith('.dist-info/METADATA')]
            if len(metadata) != 1:
                raise ReleaseError('invalid wheel metadata')
            from email.parser import BytesParser
            message = BytesParser().parsebytes(wheel.read(metadata[0]))
            from packaging.utils import canonicalize_name
            name = canonicalize_name(message['Name'])
        row = {'filename': path.name, 'name': name, 'version': message['Version'],
               'sha256': _hash_file(path)}
        wheel_rows.append(row)
        if name in product_hashes:
            raise ReleaseError('duplicate wheel distribution')
        product_hashes[name] = row['sha256']
    _provenance(provenance, product_hashes)
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.release-build-', dir=output) as work:
        work = Path(work)
        site = work / 'site'
        result = subprocess.run([sys.executable, '-m', 'pip', 'install', '--isolated',
                                 '--disable-pip-version-check', '--no-cache-dir', '--no-compile', '--no-deps',
                                 '--no-index', '--target', str(site), *map(str, wheels)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if result.returncode:
            raise ReleaseError('offline wheel installation failed')
        missing = _audit(site)
        if (site / 'astrid/core/generation/engine_release.py').read_bytes() != Path(__file__).read_bytes():
            raise ReleaseError('builder and product wheel release code differ')
        entry = (site / 'astrid/core/generation/_engine_release_entry.py').read_bytes()
        # Check real installed dispatch and the actual native/context/custody import closure.
        (work / 'run.py').write_bytes(entry)
        try:
            check = subprocess.run([sys.executable, '-I', '-S', str(work / 'run.py'), 'check'],
                                   env={'PATH': os.defpath, 'HOME': str(work),
                                        'PYTHONDONTWRITEBYTECODE': '1'},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
            checked = check.returncode == 0
        except subprocess.TimeoutExpired:
            checked = False
        if not checked:
            missing.append('isolated-canonical-cli/runtime-import-check')
        if missing and not fixture:
            raise ReleaseError('incomplete release: ' + ', '.join(sorted(missing)))
        data = {'site/' + name: content for name, content in _inventory(site).items()}
        data['run.py'] = entry
        manifest = {'format': 1, 'complete': not missing and not fixture, 'fixture': bool(fixture),
                    'python': interpreter_identity(),
                    'missing': sorted(missing), 'sources': provenance,
                    'wheels': sorted(wheel_rows, key=lambda r: r['name']),
                    'files': {n: {'sha256': digest(b), 'bytes': len(b)} for n, b in sorted(data.items())}}
        data['release.json'] = canonical(manifest)
        candidate = work / 'candidate.zip'
        with zipfile.ZipFile(candidate, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, content in sorted(data.items()):
                item = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                item.create_system = 3
                item.external_attr = (stat.S_IFREG | 0o444) << 16
                item.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(item, content)
        sha = _hash_file(candidate)
        destination = output / ('sha256-' + sha + '.zip')
        if destination.exists():
            if _hash_file(destination) != sha:
                raise ReleaseError('content-addressed destination differs')
        else:
            shutil.copyfile(candidate, destination)
            destination.chmod(0o444)
        (output / (sha + '.manifest.json')).write_bytes(canonical(manifest))
        return destination, manifest


def _members(archive):
    members = archive.infolist()
    if len(members) > MAX_FILES or len({m.filename for m in members}) != len(members):
        raise ReleaseError('duplicate or excessive archive members')
    total = 0
    for item in members:
        _relative(item.filename)
        if (item.create_system != 3 or not stat.S_ISREG(item.external_attr >> 16)
                or item.flag_bits & 1):
            raise ReleaseError('archive member is not a regular unencrypted file')
        total += item.file_size
        if total > MAX_ARCHIVE:
            raise ReleaseError('release extraction exceeds limit')
    return members


def verify_tree(root):
    """Verify every retained file before a new process imports product code."""
    root = Path(root).absolute()
    _regular(root / 'release.json')
    raw = (root / 'release.json').read_bytes()
    if len(raw) > MAX_MANIFEST:
        raise ReleaseError('manifest exceeds limit')
    manifest = _json(raw)
    if (not isinstance(manifest, dict) or set(manifest) !=
            {'format', 'complete', 'fixture', 'python', 'missing', 'sources', 'wheels', 'files'}
            or manifest['format'] != 1 or type(manifest['complete']) is not bool
            or type(manifest['fixture']) is not bool or not isinstance(manifest['files'], dict)
            or not isinstance(manifest['missing'], list)
            or not all(isinstance(item, str) for item in manifest['missing'])
            or manifest['complete'] != (not manifest['fixture'] and not manifest['missing'])):
        raise ReleaseError('invalid release manifest')
    expected = set(manifest['files']) | {'release.json'}
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ReleaseError('symlink in retained release')
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        actual.add(name)
        _regular(path)
        if name == 'release.json':
            continue
        row = manifest['files'].get(name)
        if (not isinstance(row, dict) or set(row) != {'bytes', 'sha256'}
                or type(row['bytes']) is not int or row['bytes'] < 0
                or path.stat().st_size != row['bytes'] or _hash_file(path) != row['sha256']):
            raise ReleaseError('retained release file differs')
    if actual != expected:
        raise ReleaseError('retained release member set differs')
    return manifest


@contextmanager
def _verified_archive(path, sha256):
    _regular(path)
    with path.open('rb') as source:
        before = os.fstat(source.fileno())
        h = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
        if before.st_size > MAX_ARCHIVE or h.hexdigest() != sha256:
            raise ReleaseError('archive digest differs')
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            yield archive
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ReleaseError('archive changed during verification')


def extract_release(archive_path, sha256, retained):
    """Verify archive digest and inventory, then atomically retain by digest.

    The trusted caller supplies sha256 out of band. No URL fetch is implicit.
    Reuse rechecks archive bytes AND the entire previously extracted tree.
    """
    if not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
        raise ReleaseError('expected an exact archive SHA256')
    archive_path, retained = Path(archive_path).absolute(), Path(retained).absolute()
    # Validate ancestors before creating any retained files (trusted single writer).
    if any(p.is_symlink() for p in (retained, *retained.parents)):
        raise ReleaseError('symlink in retention path')
    target = retained / ('sha256-' + sha256)
    with _verified_archive(archive_path, sha256) as archive:
        members = _members(archive)
        try:
            item = archive.getinfo('release.json')
        except KeyError:
            raise ReleaseError('missing release manifest') from None
        if item.file_size > MAX_MANIFEST:
            raise ReleaseError('manifest exceeds limit')
        manifest_bytes = archive.read(item)
        manifest = _json(manifest_bytes)
        if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
            raise ReleaseError('invalid release manifest')
        if set(manifest['files']) | {'release.json'} != {i.filename for i in members}:
            raise ReleaseError('archive member set differs from manifest')
        retained.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists():
            _regular(target / 'release.json')
            if (target / 'release.json').read_bytes() != manifest_bytes:
                raise ReleaseError('retained manifest differs from archive')
            verify_tree(target)
            return target
        with tempfile.TemporaryDirectory(prefix='.extract-', dir=retained) as temporary:
            stage = Path(temporary) / 'release'
            stage.mkdir(mode=0o700)
            for item in members:
                path = stage / _relative(item.filename)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.open(item) as source, path.open('xb') as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
                path.chmod(0o444)
            verify_tree(stage)
            os.rename(stage, target)
    return target


def _handoff_recipe(release):
    """Primary environment of the hash-indexed recipe inside a VERIFIED tree."""
    site = Path(release) / 'site/astrid'
    try:
        index = _json((site / 'packs/engine-index.json').read_bytes())
        if not isinstance(index, dict) or index.get('schema') != 'astrid.engine-index.v1':
            raise ReleaseError('invalid packaged recipe index')
        entries = [e for e in index['entries'] if isinstance(e, dict)
                   and e.get('engine') == HANDOFF_ENGINE and e.get('platform') == HANDOFF_PLATFORM]
        if len(entries) != 1 or set(entries[0]) != {'engine', 'platform', 'recipe'}:
            raise ReleaseError('release has no unique handoff recipe')
        ref = entries[0]['recipe']
        if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}
                or not isinstance(ref['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])):
            raise ReleaseError('invalid handoff recipe reference')
        path = site / _relative(ref['path'])
        _regular(path)
        raw = path.read_bytes()
        if digest(raw) != ref['sha256']:
            raise ReleaseError('handoff recipe differs from its index digest')
        recipe = _json(raw)
        if (not isinstance(recipe, dict) or recipe.get('schema') != 'astrid.engine-recipe.v1'
                or recipe.get('engine') != HANDOFF_ENGINE or recipe.get('platform') != HANDOFF_PLATFORM):
            raise ReleaseError('handoff recipe identity differs')
        primary = [e for e in recipe['environments']
                   if isinstance(e, dict) and e.get('name') == recipe.get('primary_environment')]
        if len(primary) != 1:
            raise ReleaseError('handoff recipe has no unique primary environment')
        environment = {k: primary[0][k] for k in ('relative_path', 'python_version', 'uv_version')}
    except (KeyError, TypeError, AttributeError, OSError):
        raise ReleaseError('invalid packaged handoff recipe') from None
    if not all(isinstance(v, str) for v in environment.values()):
        raise ReleaseError('invalid packaged handoff recipe')
    return environment


def expected_identity(python_version, platform_key=HANDOFF_PLATFORM):
    """Exact interpreter identity implied by a recipe pin and Linux platform key."""
    match = re.fullmatch('([0-9]+)[.]([0-9]+)[.]([0-9]+)', python_version or '')
    if match is None or not platform_key.startswith('linux-x86_64-'):
        raise ReleaseError('recipe interpreter pin is not an exact Linux x86_64 CPython')
    return {'implementation': 'cpython', 'version': [int(p) for p in match.groups()],
            'platform': 'linux', 'machine': 'x86_64'}


def _bounded(argv, *, env, end, capture=False):
    """One bootstrap child in the CALLER's process group, bounded by ``end``.

    The original agent job owns the group: its cancel signals reach these
    children. On our own bound subprocess kills/reaps the direct child.
    """
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise ReleaseError('python handoff budget exhausted')
    try:
        return subprocess.run(list(argv), env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE if capture else sys.stderr.fileno(),
                              stderr=subprocess.DEVNULL if capture else sys.stderr.fileno(),
                              timeout=remaining, check=False)
    except subprocess.TimeoutExpired:
        raise ReleaseError('python handoff step exceeded its bound') from None


def prepare_release_python(*, archive, sha256, retain, root, seconds=1800):
    """Verify a complete release, then return the exact interpreter for it.

    Order is fixed: archive digest, member set and every retained file hash;
    complete/non-fixture manifest; hash-indexed recipe; manifest identity equal
    to the recipe's managed CPython/Linux x86_64. Only then, if the running
    interpreter differs, the recipe's exact uv/managed-Python commands (shared
    with LinuxPlatformAdapter) run and the venv interpreter's live identity is
    checked. No product import, resolver, source or model step happens here.
    Returns a locator: {schema, python, release, run, bootstrapped, identity}.
    """
    if type(seconds) not in (int, float) or not 0 < seconds <= 604800:
        raise ReleaseError('invalid python handoff budget')
    end = time.monotonic() + seconds
    release = extract_release(archive, sha256, retain)
    manifest = verify_tree(release)
    if not manifest['complete'] or manifest['fixture']:
        raise ReleaseError('incomplete release cannot hand off to a managed interpreter')
    environment = _handoff_recipe(release)
    expected = expected_identity(environment['python_version'])
    if manifest['python'] != expected:
        raise ReleaseError('release interpreter differs from the recipe managed Python')
    locator = {'schema': HANDOFF_SCHEMA, 'release': str(release), 'run': str(release / 'run.py'),
               'identity': expected}
    current = interpreter_identity()
    if current == expected:
        return dict(locator, python=sys.executable, bootstrapped=False)
    if (current['platform'], current['machine']) != (expected['platform'], expected['machine']):
        raise ReleaseError('release target platform differs')
    # Exact verified retained helper; the same file LinuxPlatformAdapter imports.
    helper = runpy.run_path(str(release / 'site/astrid/core/generation/_engine_python_bootstrap.py'))
    try:
        commands = helper['linux_bootstrap_commands'](
            install_root=root, uv_version=environment['uv_version'],
            python_version=environment['python_version'],
            venv_relative_path=environment['relative_path'])
        paths = helper['linux_bootstrap_paths'](install_root=root,
                                                venv_relative_path=environment['relative_path'])
        overlay = helper['linux_bootstrap_environment'](install_root=root)
    except ValueError:
        raise ReleaseError('invalid managed interpreter bootstrap input') from None
    env = {k: os.environ[k] for k in _CHILD_ENV if k in os.environ}
    env.setdefault('PATH', os.defpath)
    env.update(overlay)
    pip_env = dict(env, **helper['linux_bootstrap_pip_environment']())
    # Refuse before any mutation when the system interpreter cannot run pip.
    if _bounded((commands[0][0], '-m', 'pip', '--version'), env=env, end=end, capture=True).returncode:
        raise ReleaseError('system python3 with pip is required by the recipe bootstrap')
    if _bounded(commands[0], env=pip_env, end=end).returncode:
        raise ReleaseError('pinned uv bootstrap failed')
    version = _bounded((paths['uv'], '--version'), env=env, end=end, capture=True)
    if version.returncode or version.stdout.decode('utf-8', 'replace').split()[:2] != [
            'uv', environment['uv_version']]:
        raise ReleaseError('bootstrap uv differs from the recipe pin')
    if _bounded(commands[1], env=env, end=end).returncode:
        raise ReleaseError('managed interpreter bootstrap failed')
    probe = _bounded((paths['python'], '-I', '-S', '-c', _IDENTITY_PROBE), env=env, end=end, capture=True)
    try:
        actual = _json(probe.stdout) if not probe.returncode and len(probe.stdout) < 4096 else None
    except ReleaseError:
        actual = None
    if actual != expected:
        raise ReleaseError('managed interpreter identity differs from the release manifest')
    return dict(locator, python=paths['python'], bootstrapped=True)


def _absolute(*paths):
    for path in paths:
        if (not isinstance(path, str) or not path.startswith('/')
                or any(part in ('', '.', '..') for part in path.split('/')[1:])
                or any(ord(c) < 32 for c in path) or '\\' in path):
            raise ReleaseError('expected a normalized absolute startup path')


def release_cli_argv(*, archive, sha256, retain, root, cli_args, seconds=1800,
                     python='/usr/bin/python3'):
    """Pure argv: verify, hand off, then exec the retained canonical CLI.

    For an existing authenticated agent job (one job owns every child). The
    CLI arguments must target the same ``--root`` as the handoff.
    """
    if not re.fullmatch('[0-9a-f]{64}', sha256 or ''):
        raise ReleaseError('expected an exact archive SHA256')
    _absolute(archive, retain, root, python)
    cli_args = tuple(cli_args)
    if not all(isinstance(a, str) for a in cli_args) or _cli_root(cli_args) != root:
        raise ReleaseError('canonical CLI arguments must select the handoff root')
    if type(seconds) not in (int, float) or not 0 < seconds <= 604800:
        raise ReleaseError('invalid python handoff budget')
    loader = Path(__file__).read_text(encoding='utf-8')
    return (python, '-I', '-S', '-c', loader, 'cli', '--archive', archive, '--sha256', sha256,
            '--retain', retain, '--root', root, '--seconds', str(seconds), '--', *cli_args)


def _cli_root(cli_args):
    roots = [cli_args[i + 1] for i, a in enumerate(cli_args[:-1]) if a == '--root']
    return roots[0] if len(roots) == 1 and '--root' not in cli_args[-1:] else None


def startup_argv(*, archive, sha256, retain, root, attempt, volume='/workspace',
                 seconds=3600, step_seconds=1800, python='/usr/bin/python3'):
    """Pure argv for an EXPLICIT provider entrypoint override, never a create.

    The small stdlib verifier travels as trusted command source; all product
    wheels/dependencies/locks travel in the separately staged release archive.
    No assumption about the image's default CMD/ENTRYPOINT is made.
    """
    if not re.fullmatch('[0-9a-f]{64}', sha256):
        raise ReleaseError('expected an exact archive SHA256')
    _absolute(archive, retain, root, volume, python)
    if any(Path(volume) not in Path(path).parents for path in (archive, retain, root)):
        raise ReleaseError('archive, retention and install root must be below the selected volume')
    if not re.fullmatch('[A-Za-z0-9_-]{1,80}', attempt):
        raise ReleaseError('invalid startup attempt')
    import math
    if any(type(n) not in (int, float) or not math.isfinite(n) or not 0 < n <= 604800
           for n in (seconds, step_seconds)):
        raise ReleaseError('invalid startup budget')
    loader = Path(__file__).read_text(encoding='utf-8')
    return (python, '-I', '-S', '-c', loader, 'start', '--archive', archive,
            '--sha256', sha256, '--retain', retain, '--volume', volume,
            '--root', root, '--attempt', attempt, '--seconds', str(seconds),
            '--step-seconds', str(step_seconds))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    build = commands.add_parser('build')
    build.add_argument('--wheel', action='append', required=True)
    build.add_argument('--provenance', required=True)
    build.add_argument('--output', required=True)
    build.add_argument('--fixture', action='store_true')
    for name in ('extract', 'start', 'prepare-python', 'cli'):
        extract = commands.add_parser(name)
        extract.add_argument('--archive', required=True)
        extract.add_argument('--sha256', required=True)
        extract.add_argument('--retain', required=True)
        if name == 'start':
            extract.add_argument('--volume', default='/workspace')
            extract.add_argument('--attempt', required=True)
            extract.add_argument('--seconds', type=float, default=3600)
            extract.add_argument('--step-seconds', type=float, default=1800)
        if name in ('start', 'prepare-python', 'cli'):
            extract.add_argument('--root', required=True)
        if name in ('prepare-python', 'cli'):
            extract.add_argument('--seconds', type=float, default=1800)
        if name == 'cli':
            extract.add_argument('cli_args', nargs='+')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'build':
            path, _ = build_release(args.wheel, _json(Path(args.provenance).read_bytes()),
                                    args.output, fixture=args.fixture)
        elif args.operation == 'extract':
            path = extract_release(args.archive, args.sha256, args.retain)
        else:
            started = time.monotonic()
            if args.operation == 'start' and any(
                    Path(args.volume) not in Path(path).parents
                    for path in (args.archive, args.retain, args.root)):
                raise ReleaseError('startup paths must remain below the selected volume')
            if args.operation == 'cli' and _cli_root(tuple(args.cli_args)) != args.root:
                raise ReleaseError('canonical CLI arguments must select the handoff root')
            prepared = prepare_release_python(archive=args.archive, sha256=args.sha256,
                                              retain=args.retain, root=args.root,
                                              seconds=args.seconds)
            if args.operation == 'prepare-python':
                print(json.dumps(prepared, sort_keys=True, separators=(',', ':')))
                return 0
            python, run = prepared['python'], prepared['run']
            if args.operation == 'cli':
                os.execv(python, [python, '-I', '-S', run, 'cli', *args.cli_args])
            # Bootstrap time is charged to the same bounded startup lifetime.
            remaining = args.seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise ReleaseError('startup budget exhausted during python handoff')
            os.execv(python, [python, '-I', '-S', run,
                     'startup', '--volume', args.volume, '--root', args.root,
                     '--attempt', args.attempt, '--seconds', str(remaining),
                     '--step-seconds', str(min(args.step_seconds, remaining))])
        print(path)
        return 0
    except (ReleaseError, OSError, zipfile.BadZipFile, subprocess.TimeoutExpired):
        print('release verification/build failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
