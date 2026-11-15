"""Supervisor-local witnesses for fully verified managed workflow requirements.

Same-user writers are trusted, as in model_verification. This file is not
tamper-proof against code running as the engine user; a separate isolation
boundary would be needed for that. Install-local storage works on local-managed
hosts and deliberately loses pod witnesses with the container. A claim ID is an
additional binding only for jobs running under a claim, never a prerequisite.
models.trust_unchanged: false disables both kinds
of witness. Metadata observations invalidate cached source/installed dependency
checks; they never claim to defend against a trusted writer forging metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import time
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

from ._engine_target_files import _TargetFiles
from .engine_contracts import EngineError

CLAIM_ID: ContextVar[str | None] = ContextVar('warm_attestation_claim', default=None)
SCHEMA = 'astrid.warm-attestation.v3'
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _receipt_identity(raw):
    # Launch refreshes health.checked_at and start/health step actions every job.
    # They describe this observation, not a change to the verified installation.
    stable = {key: value for key, value in raw.items() if key not in {'health', 'steps'}}
    stable['steps'] = [step for step in raw.get('steps', []) if step['kind'] not in {'start', 'health'}]
    return _digest(stable)


def requirement_digest(workflow, manifest):
    """Canonical requirement declarations, never graph/prompt/seed bytes."""
    from vibecomfy.runtime.dependencies import runtime_requirements_from_workflow
    runtime = runtime_requirements_from_workflow(workflow)
    # Doctor consumes typed IR requirements as well as the transport manifest
    # (for example custom-node version/schema constraints). Retain those and
    # the required class set without including graph literals or node IDs.
    declarations = {name: sorted(getattr(workflow.requirements, name), key=_digest)
                    for name in ('models', 'custom_nodes', 'custom_node_refs',
                                 'missing_models', 'missing_nodes', 'unsupported')}
    return _digest({'models': sorted(manifest['models'], key=_digest),
                    'nodes': sorted(manifest['extra']['nodes'], key=_digest),
                    'declarations': declarations,
                    'classes': sorted({node.class_type for node in workflow.nodes.values()}),
                    'runtime': runtime.to_dict() if runtime else None})


def supervisor_incarnation(client, deadline):
    """Observe the authenticated live socket peer without spawning a supervisor."""
    from .engine_custody import _directory, _vibe_birth
    with _directory(client.control, create=False) as fd:
        marker = client._marker(fd)
        if marker is None or marker['identity'] != client.identity:
            raise ValueError('supervisor identity unavailable')
        channel = client._connect(fd, deadline, start=False)
        if channel is None:
            raise ValueError('supervisor absent')
        with channel:
            pid, uid, _gid = struct.unpack('3i', channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid() or marker['process']['pid'] != pid:
                raise ValueError('supervisor peer differs')
            return {'pid': pid, 'start_ticks': _vibe_birth(pid), 'uid': uid}


def _signature(path):
    info = path.lstat()
    return [info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _tree(root, ignored, tracked, deadline, *, skip_bytecode=False, hash_bytes=False,
          symlink_root=None, ancestors=()):
    """Witness metadata, optionally content; Git objects aren't sources.

    The ignored paths are inventoried by Git only after full verification. New
    ignored paths can cause a conservative miss until the next full verification.
    Pack symlinks additionally witness their resolved content inside symlink_root.
    """
    rows, pending = [], [root]
    files = _TargetFiles(str(root))
    ignored = set(ignored) | {'.git'}
    while pending:
        directory = pending.pop()
        if time.monotonic() >= deadline:
            raise ValueError('source observation deadline')
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if skip_bytecode and (entry.name == '__pycache__' or entry.name.endswith(('.pyc', '.pyo'))):
                    continue
                if relative in ignored or relative + '/' in ignored:
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    rows.append([relative, info.st_mode, info.st_dev, info.st_ino])
                    pending.append(path)
                else:
                    rows.append([relative, *_signature(path)])
                    if hash_bytes:
                        if stat.S_ISLNK(info.st_mode):
                            rows.append([relative, 'link', os.readlink(path)])
                            if symlink_root is not None:
                                rows.append([relative, 'target',
                                             _pack_tree(path, symlink_root, deadline, ancestors)])
                        elif stat.S_ISREG(info.st_mode):
                            fact = files.file_fact(str(path), hash_bytes=True, deadline=deadline)
                            if not fact.regular or not fact.sha256:
                                raise ValueError('source content unavailable')
                            rows.append([relative, fact.sha256])
                        else:
                            raise ValueError('unsupported source member')
    # Git can ignore a whole generated directory containing tracked placeholders.
    # Always witness tracked members too, even underneath such a directory.
    for relative in tracked:
        path = root / relative
        rows.append([relative, *_signature(path)])
    return _digest(rows)


def _pack_tree(path, engine_root, deadline, ancestors=()):
    """Hash loader inputs, following only links contained in the engine root."""
    if len(ancestors) >= 64:
        raise ValueError('node pack symlink depth')
    try:
        resolved = path.resolve(strict=True)
    except RuntimeError as exc:
        raise ValueError('node pack symlink loop') from exc
    if not resolved.is_relative_to(engine_root.resolve()) or resolved in ancestors:
        raise ValueError('unsafe node pack symlink target')
    info = resolved.stat()
    if stat.S_ISDIR(info.st_mode):
        content = _tree(resolved, [], [], deadline, hash_bytes=True,
                        symlink_root=engine_root, ancestors=(*ancestors, resolved))
        signature = [info.st_mode, info.st_dev, info.st_ino]
    elif stat.S_ISREG(info.st_mode):
        fact = _TargetFiles(str(engine_root)).file_fact(str(resolved), hash_bytes=True, deadline=deadline)
        if not fact.regular or not fact.sha256:
            raise ValueError('node pack content unavailable')
        content, signature = fact.sha256, _signature(resolved)
    else:
        raise ValueError('unsupported node pack member')
    return _digest([str(resolved), signature, content])


def _node_pack_name(node):
    name = Path(urlsplit(node['repo']).path).name.removesuffix('.git')
    if not name or name in {'.', '..'} or any(c.isspace() or c in '/\\' for c in name):
        raise ValueError('warm node pack name')
    return name


def _git_identity(root, deadline):
    """Read small Git identity files, never run Git on a hit."""
    files = _TargetFiles(str(root))
    names = ['HEAD', 'index', 'packed-refs', 'config']
    git = root / '.git'
    if not git.is_dir() or git.is_symlink():
        raise ValueError('source Git directory unavailable')
    # Hash loose refs too; a symbolic HEAD may keep identical bytes on commit.
    refs = git / 'refs'
    if refs.exists():
        for directory, dirs, leaves in os.walk(refs, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in dirs):
                raise ValueError('unsafe source refs')
            names.extend(str((Path(directory) / name).relative_to(git)) for name in leaves)
    result = {}
    for name in sorted(names):
        fact = files.file_fact(str(git / name), hash_bytes=True, deadline=deadline)
        if fact.symlink or fact.exists and not fact.regular:
            raise ValueError('unsafe Git identity file')
        result[name] = fact.sha256
    if not result['HEAD']:
        raise ValueError('source HEAD unavailable')
    return _digest(result)


def _dependencies(root, recipe, deadline):
    """Stat installed code and metadata instead of launching Python/pip probes."""
    rows = []
    for environment in recipe.environments:
        base = root / environment.relative_path
        with _TargetFiles(str(root))._directory(base.parts[1:], deadline):
            pass
        rows.append([environment.name, _tree(base, [], [], deadline, skip_bytecode=True)])
        # Unix managed venvs; absent metadata is a conservative miss.
        sites = sorted(base.glob('lib/python*/site-packages'))
        if not sites:
            raise ValueError('installed dependency metadata absent')
        for site in sites:
            if time.monotonic() >= deadline:
                raise ValueError('dependency observation deadline')
            with _TargetFiles(str(root))._directory(site.parts[1:], deadline):
                pass
            for metadata in sorted(site.glob('*.dist-info')):
                if metadata.is_symlink():
                    raise ValueError('unsafe dependency metadata')
                for name in ('METADATA', 'RECORD', 'direct_url.json'):
                    path = metadata / name
                    if path.is_symlink() or path.exists() and not path.is_file():
                        raise ValueError('unsafe dependency metadata file')
                    if name == 'METADATA' and not path.is_file():
                        raise ValueError('dependency metadata absent')
                    rows.append([str(path.relative_to(root)), _signature(path) if path.exists() else None])
    return _digest(rows)


class WarmAttestation:
    def __init__(self, root, recipe, runner, claim_id, deadline, *, enabled):
        self.root, self.recipe, self.runner = Path(root), recipe, runner
        self.claim_id, self.deadline = claim_id, deadline
        self.enabled = enabled is True and (claim_id is None or isinstance(claim_id, str) and bool(claim_id))
        self.files = _TargetFiles(str(self.root))
        self.path = str(self.root / '.astrid-engine/warm-attestation.json')
        self.saved = None
        self.known_node_packs = []
        self.matched = False
        if self.enabled:
            try:
                saved = self.files.read_json(self.path, deadline=deadline)
                self._validate(saved)
                self.known_node_packs = saved['node_packs']
                if (_digest(self.identity(saved['sources'], saved['node_packs'])) == _digest(saved['identity'])
                        and _digest(self._model_stats(saved['model_stats'])) == _digest(saved['model_stats'])):
                    self.saved, self.matched = saved, True
            except (OSError, ValueError, TypeError, KeyError, AttributeError, EngineError, struct.error):
                pass  # An optional witness never turns an observation error into trust.

    @staticmethod
    def _validate(saved):
        keys = {'schema', 'identity', 'sources', 'node_packs', 'requirements', 'engine_port', 'object_info_sha256',
                'model_rows', 'model_stats', 'protected_packages'}
        if not isinstance(saved, dict) or set(saved) != keys or saved['schema'] != SCHEMA:
            raise ValueError('warm attestation schema')
        if (not isinstance(saved['identity'], dict) or not isinstance(saved['sources'], dict)
                or type(saved['engine_port']) is not int or not 0 < saved['engine_port'] < 65536
                or saved['object_info_sha256'] is not None and (not isinstance(saved['object_info_sha256'], str)
                    or not _SHA.fullmatch(saved['object_info_sha256']))
                or not isinstance(saved['requirements'], list) or not saved['requirements']
                or any(not isinstance(item, str) or not _SHA.fullmatch(item) for item in saved['requirements'])
                or len(set(saved['requirements'])) != len(saved['requirements'])
                or not isinstance(saved['protected_packages'], list)
                or any(not isinstance(item, str) for item in saved['protected_packages'])
                or not isinstance(saved['model_rows'], list) or not isinstance(saved['model_stats'], dict)):
            raise ValueError('warm attestation types')
        if (not isinstance(saved['node_packs'], list)
                or any(not isinstance(node, dict) or set(node) != {'repo', 'commit'}
                       or any(not isinstance(value, str) or not value for value in node.values())
                       for node in saved['node_packs'])):
            raise ValueError('warm node pack declarations')
        for source in saved['sources'].values():
            if (not isinstance(source, dict) or set(source) != {'ignored', 'tracked'}
                    or any(not isinstance(source[key], list) for key in ('ignored', 'tracked'))
                    or any(not isinstance(path, str) or not path or Path(path).is_absolute()
                           or '..' in Path(path).parts for key in ('ignored', 'tracked') for path in source[key])):
                raise ValueError('warm source witness')
        # Comparing identity dictionaries below enforces exact keys and types by
        # construction; reject bools in integer positions (Python True == 1).
        supervisor = saved['identity'].get('supervisor')
        if (not isinstance(supervisor, dict) or set(supervisor) != {'pid', 'start_ticks', 'uid'}
                or type(supervisor['pid']) is not int or type(supervisor['uid']) is not int
                or supervisor['pid'] <= 0 or supervisor['uid'] < 0
                or not isinstance(supervisor['start_ticks'], str) or not supervisor['start_ticks'].isdigit()):
            raise ValueError('warm supervisor witness')
        if 'claim_id' in saved['identity'] and (not isinstance(saved['identity']['claim_id'], str)
                                               or not saved['identity']['claim_id']):
            raise ValueError('warm claim witness')
        for row in saved['model_rows']:
            if (not isinstance(row, dict) or set(row) != {'name', 'subdir', 'url', 'sha256', 'size'}
                    or not isinstance(row['name'], str) or not row['name'] or Path(row['name']).name != row['name']
                    or not isinstance(row['subdir'], str) or Path(row['subdir']).is_absolute()
                    or '..' in Path(row['subdir']).parts
                    or row['url'] is not None and not isinstance(row['url'], str)
                    or not isinstance(row['sha256'], str) or not _SHA.fullmatch(row['sha256'].removeprefix('sha256:'))
                    or type(row['size']) is not int or row['size'] < 0):
                raise ValueError('warm model declaration')

    def _node_pack_paths(self):
        """Cover imports and prestartup scripts, independently of Git ignores."""
        comfy = next(source for source in self.recipe.sources if source.name == 'comfyui')
        root = self.root / comfy.relative_path / 'custom_nodes'
        with self.files._directory(root.parts[1:], self.deadline):
            pass
        with os.scandir(root) as entries:
            return {entry.name: Path(entry.path) for entry in entries
                    if not entry.name.endswith('.disabled')
                    and not (entry.is_file() and Path(entry.name).suffix != '.py')}

    def _loader_paths(self):
        """Refuse unobserved custom_nodes roots instead of trusting a partial scan."""
        launch = self.runner.launch_spec()
        for env in (os.environ, getattr(self.runner, 'env', {}), launch.get('env', {})):
            if 'COMFYUI_PATH' in env and env['COMFYUI_PATH'] is not None:
                raise ValueError('warm loader root override')
            raw = env.get('VIBECOMFY_COMFY_CONFIGURATION')
            if raw is not None:
                config = json.loads(raw)
                if not isinstance(config, dict) or {'base_directory', 'extra_model_paths_config'} & config.keys():
                    raise ValueError('warm loader path configuration')
        if any(arg.split('=', 1)[0] in {'--base-directory', '--extra-model-paths-config'}
               for arg in launch['argv']):
            raise ValueError('warm loader path argument')
        comfy = next(source for source in self.recipe.sources if source.name == 'comfyui')
        root = self.root / comfy.relative_path
        # main.py reads this automatically; VibeComfy also checks its session cwd.
        # Observe absence explicitly even when Git ignores the configuration file.
        paths = {root / 'extra_model_paths.yaml', Path(launch.get('cwd', root)) / 'extra_model_paths.yaml'}
        presence = {}
        for path in sorted(paths):
            fact = self.files.file_fact(str(path), hash_bytes=False, deadline=self.deadline)
            presence[str(path.relative_to(self.root))] = fact.exists
            if fact.exists:
                raise ValueError('warm extra loader paths present')
        return presence

    def _node_packs(self, nodes):
        """Witness every installed loader member, including unpinned/non-Git packs."""
        declarations = {}
        for node in nodes:
            name = _node_pack_name(node)
            if name in declarations:
                raise ValueError('warm node pack name')
            declarations[name] = node
        paths = self._node_pack_paths()
        if declarations.keys() - paths.keys():
            raise ValueError('declared node pack absent')
        rows = []
        for name, path in sorted(paths.items()):
            tree_sha256 = _pack_tree(path, self.root, self.deadline)
            root = path.resolve(strict=True)
            git_sha256, observed = None, [None, None]
            if (root / '.git').exists() or (root / '.git').is_symlink():
                git_sha256 = _git_identity(root, self.deadline)
                for index, args in enumerate([('rev-parse', 'HEAD'),
                        ('status', '--porcelain=v1', '--untracked-files=all', '-z')]):
                    result = self.runner.run(('git', '-C', str(root), *args), check=False)
                    if result.exit_code != 0 or result.stdout_truncated:
                        raise ValueError('warm node pack Git observation')
                    observed[index] = result.stdout.strip() if index == 0 else result.stdout
            rows.append({'name': name, **declarations.get(name, {}),
                         'head': observed[0], 'porcelain': observed[1],
                         'git_sha256': git_sha256,
                         'tree_sha256': tree_sha256})
        return rows

    def identity(self, sources, node_packs):
        from .engine_contracts import effective_models_root
        from .engine_recipe import recipe_digest
        loader_paths = self._loader_paths()
        raw = self.files.read_json(str(self.root / '.astrid-engine/install.json'), deadline=self.deadline)
        models_root = Path(effective_models_root(str(self.root), raw.get('models_root')))
        source_rows = {}
        if set(sources) != {source.name for source in self.recipe.sources}:
            raise ValueError('warm source set')
        for source in self.recipe.sources:
            root = self.root / source.relative_path
            source_rows[source.name] = {'head': source.pin.revision, 'porcelain_clean': True,
                'git_sha256': _git_identity(root, self.deadline),
                'tree_sha256': _tree(root, sources[source.name]['ignored'], sources[source.name]['tracked'], self.deadline)}
        locks, node_locks = {}, {}
        for environment in self.recipe.environments:
            path = self.root / '.astrid-engine/environments' / environment.name / 'requirements.lock'
            fact = self.files.file_fact(str(path), hash_bytes=True, deadline=self.deadline)
            if not fact.regular or fact.symlink or not fact.sha256:
                raise ValueError('warm environment lock')
            locks[environment.name] = fact.sha256
            if environment.node_lock is not None:
                ref = environment.node_lock
                source = next(source for source in self.recipe.sources if source.name == ref.source)
                node = self.files.file_fact(str(self.root / source.relative_path / ref.path),
                                            hash_bytes=True, deadline=self.deadline)
                if not node.regular or node.symlink or node.sha256 != ref.sha256:
                    raise ValueError('warm node lock differs from pinned source file')
                node_locks[environment.name] = node.sha256
        info = models_root.stat()
        if models_root.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise ValueError('warm models root')
        return {**({'claim_id': self.claim_id} if self.claim_id is not None else {}),
                'supervisor': supervisor_incarnation(self.runner.context.service_client, self.deadline),
                'recipe_sha256': recipe_digest(self.recipe), 'helper_sha256': self.runner.helper_sha256,
                'receipt_sha256': _receipt_identity(raw), 'sources': source_rows, 'requirements_locks': locks,
                'node_locks': node_locks,
                'loader_paths': loader_paths,
                'node_packs': self._node_packs(node_packs),
                'runtime_dependency_sha256': _dependencies(self.root, self.recipe, self.deadline),
                'models_root_receipt': {'root': str(models_root), 'receipt_id': raw['receipt_id'],
                                        'dev': info.st_dev, 'inode': info.st_ino}}

    def _models(self, rows):
        # Host-route model_rows are recorded for the later routing card. Live
        # matching reads only the separate path/stat witnesses below.
        records = {_digest(row): {'path': (Path(row['subdir']) / row['name']).as_posix(), 'stat': []}
                   for row in rows}
        return self._model_stats(records)

    def _model_stats(self, records):
        from .engine_contracts import effective_models_root
        raw = self.files.read_json(str(self.root / '.astrid-engine/install.json'), deadline=self.deadline)
        root = Path(effective_models_root(str(self.root), raw.get('models_root')))
        result = {}
        for key, record in records.items():
            if (not isinstance(key, str) or not _SHA.fullmatch(key) or not isinstance(record, dict)
                    or set(record) != {'path', 'stat'} or not isinstance(record['path'], str)):
                raise ValueError('warm model witness')
            relative = Path(record['path'])
            path = root / relative
            if not relative.parts or relative.is_absolute() or '..' in relative.parts:
                raise ValueError('warm model path')
            # Guard parent aliases as well as the leaf, without hashing model bytes.
            with _TargetFiles(str(root))._directory(path.parts[1:-1], self.deadline):
                pass
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('warm model kind')
            result[key] = {'path': record['path'], 'stat': _signature(path)}
        return result

    def contains(self, requirement):
        return self.matched and requirement in self.saved['requirements']

    def write(self, requirement, manifest, *, object_info_sha256, protected_packages):
        """Only the full-verification success path may extend this witness."""
        if not self.enabled:
            return
        try:
            sources = self.saved['sources'] if self.matched else {}
            if not self.matched:
                for source in self.recipe.sources:
                    root = self.root / source.relative_path
                    prefix = ('git', '-C', str(root))
                    result = self.runner.run((*prefix,
                        'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z'))
                    if result.stdout_truncated:
                        raise ValueError('warm source inventory truncated')
                    ignored = set(filter(None, result.stdout.split('\0')))
                    result = self.runner.run((*prefix, 'ls-files', '--cached', '-z'))
                    if result.stdout_truncated:
                        raise ValueError('warm tracked inventory truncated')
                    tracked = sorted(filter(None, result.stdout.split('\0')))
                    # Persist ignored directory roots, not just yesterday's
                    # generated output filenames. No Git invocation on a hit.
                    parents = {parent.as_posix() + '/' for name in [*ignored, *tracked]
                               for parent in Path(name).parents if str(parent) != '.'}
                    if parents:
                        result = self.runner.run((*prefix, 'check-ignore', '--no-index', '--', *sorted(parents)),
                                                 check=False)
                        if result.exit_code not in {0, 1} or result.stdout_truncated:
                            raise ValueError('warm ignored directory inventory')
                        directories = result.stdout.splitlines()
                        if any(directory not in parents for directory in directories):
                            raise ValueError('warm ignored directory encoding')
                        ignored.update(directories)
                    sources[source.name] = {'ignored': sorted(ignored), 'tracked': tracked}
            installed = self._node_pack_paths()
            nodes = {node['repo']: node for node in self.known_node_packs if _node_pack_name(node) in installed}
            nodes.update({node['repo']: node for node in manifest['extra']['nodes']})
            node_packs = [nodes[repo] for repo in sorted(nodes)]
            identity = self.identity(sources, node_packs)
            rows = {_digest(row): row for row in manifest['models']}
            requirements = {requirement}
            if self.matched:
                # Adding a pack may extend the attested requirement set only if
                # the base and every previously attested pack remain unchanged.
                prior_names = {node['name'] for node in self.saved['identity']['node_packs']}
                prior_identity = {**identity, 'node_packs': [node for node in identity['node_packs']
                                                            if node['name'] in prior_names]}
                if prior_identity == self.saved['identity']:
                    requirements.update(self.saved['requirements'])
                    rows.update({_digest(row): row for row in self.saved['model_rows']})
            rows = [rows[key] for key in sorted(rows)]
            body = {'schema': SCHEMA, 'identity': identity, 'sources': sources, 'node_packs': node_packs,
                    'requirements': sorted(requirements), 'engine_port': self.recipe.start.port,
                    'object_info_sha256': object_info_sha256, 'model_rows': rows,
                    'model_stats': self._models(rows), 'protected_packages': list(protected_packages)}
            self._validate(body)
            self.files.write_json_atomic(self.path, body, deadline=self.deadline)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, EngineError, struct.error):
            pass  # An unavailable optimization must not spoil a fully verified job.


def collection_receipt(*, root, engine, platform, claim_id, deadline, trust_unchanged):
    """Optional pod-local preparation/collection status shortcut under install custody.

    The controller still validates and independently reads the canonical receipt.
    Misses (including an unreadable witness) retain canonical status verification.
    """
    if trust_unchanged is not True or not isinstance(claim_id, str) or not claim_id:
        return None
    from .engine_execution import create_local_backend
    from .engine_recipe import load_recipe
    from .engine_runner import (
        _parse_receipt,
        _Runner,
        plan_from_receipt,
        plan_install,
        require_current_receipt,
    )
    try:
        recipe = load_recipe(engine, platform)
        context, adapter = create_local_backend(engine=engine, platform=platform, placement='local',
                                                root=root, deadline=deadline)
        with adapter.local_install_lock(root, deadline=deadline, cancelled=lambda: False):
            raw = context.read_json(str(Path(root) / '.astrid-engine/install.json'), deadline=deadline)
            receipt = _parse_receipt(raw, plan_install(recipe, root=root, placement='local'))
            plan = plan_from_receipt(recipe, receipt, root=root, placement='local')
            require_current_receipt(receipt, plan, recipe)
            if receipt.status != 'installed':
                return None
            runner = _Runner(plan, context, adapter, deadline)
            if runner.helper('identity') != {'helper_sha256': runner.helper_sha256}:
                return None
            warm = WarmAttestation(root, recipe, runner, claim_id, deadline, enabled=True)
            return raw if warm.matched else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EngineError, struct.error):
        return None
