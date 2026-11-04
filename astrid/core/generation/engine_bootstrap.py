"""Bounded exact-source preparation for the finite engines planning CLI.

Native execution and long-lived helper source ownership require A2/P2 review.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import io
import json
import re
import tomllib
import zipfile
import lzma
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from .engine_contracts import IntegrityError, RecipeValidationError

_SOURCE_LIMIT = 65536
_ARCHIVE_LIMIT = 524288
_SPEC_LIMIT = 16384
_TOTAL_LIMIT = 98304
_FILES = (
    'astrid/__init__.py', 'astrid/version.py', 'astrid/console.py',
    'astrid/core/__init__.py', 'astrid/core/engine_cli.py',
    'astrid/core/generation/__init__.py', 'astrid/core/generation/engine_contracts.py',
    'astrid/core/generation/engine_recipe.py', 'astrid/core/generation/engine_runner.py',
    'astrid/core/generation/engine_local_endpoint.py',
    'astrid/core/generation/engine_runner_target.py', 'astrid/core/generation/model_downloader.py',
    'astrid/packs/engine-index.json',
    'astrid/packs/runpod/recipe.linux-cu130.json', 'astrid/packs/runpod/pins.json',
    'astrid/packs/runpod/models.lock.json',
    'astrid/packs/vibecomfy/recipe.windows-cu130.json', 'astrid/packs/vibecomfy/pins.json',
    'astrid/packs/vibecomfy/models.lock.json',
    'astrid/packs/wan2gp/recipe.windows-cu128.json', 'astrid/packs/wan2gp/pins.json',
    'astrid/packs/wan2gp/models.lock.json',
)


@dataclass(frozen=True)
class CliBootstrap:
    schema: Literal['astrid.engine-cli-bootstrap.v1']
    env: Mapping[str, str]
    command: tuple[str, ...]
    source_sha256: str
    files: Mapping[str, str]


def _validate_argv(argv):
    # Only a closed, finite planning surface is deliverable before A2/P2.
    if not isinstance(argv, tuple) or not all(isinstance(x, str) for x in argv):
        raise RecipeValidationError('argv must be a tuple of public strings')
    if argv in (('engines', '--help'), ('engines', '-h'),
                ('engines', 'install', '--help'), ('engines', 'status', '--help')):
        return
    if len(argv) < 4 or argv[:2] != ('engines', 'install') or '--dry-run' not in argv:
        raise RecipeValidationError('only engines dry-run or help is available')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', argv[2]):
        raise RecipeValidationError('invalid engine')
    seen = set()
    i = 3
    while i < len(argv):
        option = argv[i]
        if option in seen or option not in ('--dry-run', '--platform', '--placement', '--root', '--deadline'):
            raise RecipeValidationError('invalid public option')
        seen.add(option)
        if option == '--dry-run':
            i += 1
            continue
        if i + 1 >= len(argv):
            raise RecipeValidationError('missing public option value')
        value = argv[i + 1]
        patterns = {'--platform': r'(linux|windows)-x86_64-cu(124|128|130)',
                    '--placement': r'(local|runpod)', '--deadline': r'[0-9]{1,6}(\.[0-9]{1,6})?',
                    '--root': r'[/A-Za-z0-9_.:-]{1,512}'}
        if not re.fullmatch(patterns[option], value):
            raise RecipeValidationError('invalid public option value')
        i += 2


def build_cli_bootstrap(argv: tuple[str, ...]) -> CliBootstrap:
    _validate_argv(argv)
    root = Path(__file__).resolve().parents[3]
    try:
        data = {}
        for name in _FILES:
            path = root / name
            if any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
                raise IntegrityError('symlink in bootstrap closure')
            if not path.is_file():
                raise IntegrityError('missing bootstrap closure')
            data[name] = path.read_bytes()
        candidates = list(importlib.metadata.distributions(name='astrid', path=[str(root)]))
        if len(candidates) != 1:
            raise IntegrityError('missing or ambiguous distribution metadata')
        dist = candidates[0]
        if not isinstance(dist, importlib.metadata.PathDistribution):
            raise IntegrityError('unsupported distribution metadata')
        metadata_root = Path(dist._path)
        if metadata_root.name.endswith('.egg-info'):
            metadata_name = 'PKG-INFO'
        elif metadata_root.name.endswith('.dist-info'):
            metadata_name = 'METADATA'
        else:
            raise IntegrityError('unsupported distribution metadata')
        if metadata_root.is_symlink():
            raise IntegrityError('symlink in distribution metadata')
        metadata = {}
        for source_name, target_name in ((metadata_name, 'METADATA'),
                                         ('entry_points.txt', 'entry_points.txt')):
            path = metadata_root / source_name
            if path.is_symlink() or not path.is_file():
                raise IntegrityError('missing installed distribution metadata')
            metadata[target_name] = path.read_bytes()
        version = dist.version
        if (dist.metadata.get('Name', '') != 'astrid' or
                not isinstance(version, str) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.!+_-]*', version)):
            raise IntegrityError('invalid installed distribution metadata')
        from astrid.version import __version__
        project_file = root / 'pyproject.toml'
        expected_version = (__version__ if not project_file.is_file() else
                            tomllib.loads(project_file.read_text(encoding='utf-8'))['project']['version'])
        if version != expected_version:
            raise IntegrityError('distribution version mismatch')
        entries = [e for e in dist.entry_points if e.group == 'console_scripts' and e.name == 'astrid']
        if len(entries) != 1 or entries[0].value != 'astrid.console:main':
            raise IntegrityError('installed console entry point mismatch')
        for name, content in metadata.items():
            data[f'astrid-{version}.dist-info/{name}'] = content
    except (OSError, importlib.metadata.PackageNotFoundError) as exc:
        raise IntegrityError('missing bootstrap closure') from exc
    files = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(data.items())}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as bundle:
        for name, content in sorted(data.items()):
            item = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            bundle.writestr(item, content)
    archive = output.getvalue()
    if len(archive) > _ARCHIVE_LIMIT:
        raise IntegrityError('bootstrap archive budget exceeded')
    source = base64.b64encode(lzma.compress(archive, preset=6)).decode('ascii')
    if len(source) > _SOURCE_LIMIT:
        raise IntegrityError('bootstrap source budget exceeded')
    source_hash = hashlib.sha256(archive).hexdigest()
    spec = json.dumps({'argv': argv, 'files': files, 'source_sha256': source_hash},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    if len(spec) > _SPEC_LIMIT:
        raise RecipeValidationError('bootstrap spec budget exceeded')
    try:
        loader = Path(__file__).with_name('_engine_bootstrap_loader.py').read_text(encoding='utf-8')
    except OSError as exc:
        raise IntegrityError('missing bootstrap loader') from exc
    loader = loader.replace('@SPEC_SHA256@', hashlib.sha256(spec).hexdigest())
    env = {'ASTRID_ENGINE_SOURCE': source, 'ASTRID_ENGINE_SPEC': base64.b64encode(spec).decode('ascii')}
    command = ('python3', '-I', '-S', '-c', loader)
    if sum(len(k.encode()) + len(v.encode()) + 2 for k, v in env.items()) + sum(len(x.encode()) + 1 for x in command) > _TOTAL_LIMIT:
        raise IntegrityError('bootstrap total budget exceeded')
    return CliBootstrap('astrid.engine-cli-bootstrap.v1', MappingProxyType(env), command,
                        source_hash, MappingProxyType(files))
