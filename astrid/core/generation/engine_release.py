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
import shutil
import stat
import subprocess
import sys
import tempfile
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
    'astrid/core/generation/engine_startup.py',
    'astrid/core/generation/engine_execution.py',
    'astrid/core/generation/_engine_target_files.py',
    'astrid/core/generation/engine_custody.py',
    'astrid/core/generation/engine_runner_target.py',
    'runtime_protocol/platform.py', 'runtime_protocol/cancellation.py',
    'runpod_lifecycle/pod_agent.py',
)


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


def startup_argv(*, archive, sha256, retain, root, attempt, volume='/workspace',
                 seconds=3600, step_seconds=1800, python='/usr/bin/python3'):
    """Pure argv for an EXPLICIT provider entrypoint override, never a create.

    The small stdlib verifier travels as trusted command source; all product
    wheels/dependencies/locks travel in the separately staged release archive.
    No assumption about the image's default CMD/ENTRYPOINT is made.
    """
    if not re.fullmatch('[0-9a-f]{64}', sha256):
        raise ReleaseError('expected an exact archive SHA256')
    for path in (archive, retain, root, volume, python):
        if (not isinstance(path, str) or not path.startswith('/')
                or any(part in ('', '.', '..') for part in path.split('/')[1:])
                or any(ord(c) < 32 for c in path) or '\\' in path):
            raise ReleaseError('expected a normalized absolute startup path')
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
    for name in ('extract', 'start'):
        extract = commands.add_parser(name)
        extract.add_argument('--archive', required=True)
        extract.add_argument('--sha256', required=True)
        extract.add_argument('--retain', required=True)
        if name == 'start':
            extract.add_argument('--volume', default='/workspace')
            extract.add_argument('--root', required=True)
            extract.add_argument('--attempt', required=True)
            extract.add_argument('--seconds', type=float, default=3600)
            extract.add_argument('--step-seconds', type=float, default=1800)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'build':
            path, _ = build_release(args.wheel, _json(Path(args.provenance).read_bytes()),
                                    args.output, fixture=args.fixture)
        else:
            if args.operation == 'start' and any(
                    Path(args.volume) not in Path(path).parents
                    for path in (args.archive, args.retain, args.root)):
                raise ReleaseError('startup paths must remain below the selected volume')
            path = extract_release(args.archive, args.sha256, args.retain)
            if args.operation == 'start':
                manifest = verify_tree(path)
                if not manifest['complete'] or manifest['fixture']:
                    raise ReleaseError('incomplete release cannot self-start')
                os.execv(sys.executable, [sys.executable, '-I', '-S', str(path / 'run.py'),
                         'startup', '--volume', args.volume, '--root', args.root,
                         '--attempt', args.attempt, '--seconds', str(args.seconds),
                         '--step-seconds', str(args.step_seconds)])
        print(path)
        return 0
    except (ReleaseError, OSError, zipfile.BadZipFile, subprocess.TimeoutExpired):
        print('release verification/build failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
