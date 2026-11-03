"""Inspect and align catalog assets on an existing managed Comfy target.

Inspection is offline. A missing target-specific schema cache is unknown, never
proof of readiness. Apply requires fresh user_env consent and re-probes the same
endpoint; it never provisions, starts or restarts an engine.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .asset_gaps import align_argv, workflow_asset_gaps
from .engine_contracts import EngineError, IntegrityError, ModelSpec, UnsafePathError
from .engine_recipe import load_model_lock, load_recipe, load_recipe_index
from .model_downloader import _matches, _root_and_destination, _safe_relative, _validate_model, download_model


class AlignmentError(EngineError):
    code = 'asset_alignment_failed'


@dataclass(frozen=True)
class ManagedAssetTarget:
    root: Path
    models_root: Path
    custom_nodes_root: Path
    python: str
    url: str
    models_lock_sha256: str
    schema_cache: Path
    vibecomfy_root: Path | None = None
    receipt_sha256: str | None = None
    locked_models: tuple[ModelSpec, ...] = ()

    def object_info(self) -> dict[str, Any]:
        from .backends.comfy_target import ComfyTarget, ComfyTargetClient
        return ComfyTargetClient(ComfyTarget(self.url, 'managed')).object_info()

    def cached_object_info(self) -> dict[str, Any] | None:
        from vibecomfy.schema.cache import CACHE_METADATA_KEY, load_object_info_cache, object_info_payload, validate_object_info_cache
        _safe_path(self.root, self.schema_cache)
        data = load_object_info_cache(self.schema_cache)
        if data is None:
            return None
        result = validate_object_info_cache(data, expected={'server_url': self.url},
                                            cache_path=self.schema_cache, policy='strict')
        if not result.ok:
            return None
        if self.receipt_sha256 is not None and data[CACHE_METADATA_KEY].get('managed_target_receipt_sha256') != self.receipt_sha256:
            return None
        return object_info_payload(data) if result.ok else None

    def cache_object_info(self, observed: dict[str, Any]) -> None:
        from vibecomfy.schema.cache import write_object_info_cache
        _safe_path(self.root, self.schema_cache)
        write_object_info_cache(self.schema_cache, observed, server_url=self.url,
            metadata={'managed_target_receipt_sha256': self.receipt_sha256})

    def verify_installation(self, *, deadline: float) -> None:
        """Keep existing source/environment integrity gates before executing code.

        The model and live-node checks are the gaps this operation repairs;
        source and environment checks continue to use the installed machinery.
        """
        from .engine_execution import create_local_backend
        from .engine_runner import _Runner, _parse_receipt, plan_install

        raw = json.loads(_safe_path(self.root, self.root / '.astrid-engine/install.json').read_bytes())
        recipe = load_recipe(raw['engine'], raw['platform'])
        plan = plan_install(recipe, root=str(self.root), placement='local')
        _parse_receipt(raw, plan)
        context, adapter = create_local_backend(engine=recipe.engine, platform=recipe.platform,
            placement='local', root=str(self.root), deadline=deadline)
        runner = _Runner(plan, context, adapter, deadline)
        if runner.helper('identity') != {'helper_sha256': runner.helper_sha256}:
            raise IntegrityError('managed target helper identity differs; repair the engine installation')
        for source in recipe.sources:
            if '/custom_nodes/' not in source.relative_path:
                runner.source(source, readonly=True)
        for environment in recipe.environments:
            runner.environment(environment, readonly=True)
        if runner.helper('models-config-check') != {'valid': True}:
            raise IntegrityError('managed target models configuration differs; repair the engine installation')

    def install_lock(self, *, deadline: float):
        from .engine_platforms import platform_adapter
        raw = json.loads(_safe_path(self.root, self.root / '.astrid-engine/install.json').read_bytes())
        recipe = load_recipe(raw['engine'], raw['platform'])
        return platform_adapter(recipe.platform).local_install_lock(str(self.root), deadline=deadline)

    def verify_readiness(self, *, deadline: float) -> None:
        from .engine_local_endpoint import local_engine_endpoint
        installed = local_engine_endpoint('comfyui', root=str(self.root),
            deadline_seconds=max(0.001, min(120, deadline - time.monotonic())))
        if installed.endpoint.url != self.url:
            raise IntegrityError('managed target endpoint changed during reconciliation')


def _safe_path(root: Path, path: Path) -> Path:
    """Reuse the downloader's ancestor/symlink checks for every managed path."""
    relative = path.relative_to(root).as_posix()
    if path.is_dir():
        _root_and_destination(str(path), PurePosixPath('.asset-path-check'))
    else:
        _root_and_destination(str(root), _safe_relative(relative))
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise UnsafePathError(f'managed asset has a hard-link alias: {relative}')
    return path


def selected_target() -> ManagedAssetTarget:
    from .engine_local_endpoint import recorded_engine_root
    from .engine_runner import _parse_receipt, plan_install
    from vibecomfy.schema.cache import object_info_cache_path

    # This is the same installed-receipt authority used by target selection,
    # without engine_status (which probes and can mutate custody state).
    registered = recorded_engine_root('comfyui')
    if registered is None:
        raise AlignmentError('No installed managed ComfyUI target; install ComfyUI before running astrid generate align')
    root = Path(registered)
    receipt_path = _safe_path(root, root / '.astrid-engine/install.json')
    raw = json.loads(receipt_path.read_bytes())
    recipe = load_recipe(raw['engine'], raw['platform'])
    receipt = _parse_receipt(raw, plan_install(recipe, root=str(root), placement='local'))
    if receipt.engine != 'comfyui' or receipt.status != 'installed':
        raise IntegrityError('managed target needs an installed ComfyUI receipt before running astrid generate align')
    if receipt.models_lock_sha256 != recipe.models_lock.sha256:
        raise IntegrityError('managed target models_lock_sha256 differs from its verified recipe')
    comfy = root / next(source.relative_path for source in recipe.sources if source.name == 'comfyui')
    url = f'http://127.0.0.1:{recipe.start.port}'
    models = root / 'models'
    nodes = comfy / 'custom_nodes'
    # Validate directories without creating them; downloader validates again at use.
    for directory in (models, nodes):
        _root_and_destination(str(directory), PurePosixPath('.asset-path-check'))
    cache = object_info_cache_path(server_url=url, cache_dir=root / '.astrid-engine/schema-cache')
    vibecomfy = root / next(source.relative_path for source in recipe.sources if source.name == 'vibecomfy')
    environment = next(row for row in recipe.environments if row.name == recipe.primary_environment)
    python = str(root / environment.relative_path / ('Scripts/python.exe' if recipe.platform.startswith('windows-') else 'bin/python'))
    return ManagedAssetTarget(root, models, nodes, python, url,
                              receipt.models_lock_sha256, cache, vibecomfy,
                              hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                              load_model_lock(recipe.models_lock).models)


def _locked_models() -> dict[str, ModelSpec]:
    """Load only digest-verified packaged locks; do not enrich from the network."""
    result = {}
    for indexed in load_recipe_index().entries:
        recipe = load_recipe(indexed.engine, indexed.platform)
        for model in load_model_lock(recipe.models_lock).models:
            previous = result.get(model.destination)
            if previous is not None and previous != model:
                raise IntegrityError(f'conflicting packaged model locks: {model.destination}')
            result[model.destination] = model
    return result


def catalog_manifests(entry_id: str | None = None) -> list[dict[str, Any]]:
    from astrid.core.model_catalog.registry import ModelRegistry
    from .backends.vibecomfy import _load_pinned_catalog_workflow
    from vibecomfy.model_assets import _referenced_model_values, _subdir_for_model_reference
    from vibecomfy.node_packs import resolve_node_packs, read_lockfile
    from vibecomfy.node_packs._lockfile import resolve_lockfile_path

    locked = _locked_models()
    lockpath = resolve_lockfile_path()
    node_locks = {row.name: row for row in read_lockfile(lockpath)}
    entries = ModelRegistry.load_default().list_all(include_closed=True)
    if entry_id is not None:
        entries = [entry for entry in entries if entry.id == entry_id]
        if not entries:
            raise AlignmentError(f'Unknown catalog entry {entry_id}; astrid generate align requires a catalog entry ID')
    manifests = []
    for entry in entries:
        modes = []
        for mode, spec in entry.modes.items():
            backend = spec.backends.get('local')
            if backend is None:
                continue
            wf = _load_pinned_catalog_workflow(backend)
            models, unlocked = {}, []
            for ref in _referenced_model_values(wf):
                folder = _subdir_for_model_reference(ref['class_type'], ref['field'])
                destination = f"{folder}/{ref['value']}" if folder else ref['value']
                model = locked.get(destination)
                if model is None:
                    unlocked.append({'kind': 'model', **ref, 'destination': destination,
                                     'missing': ['locked_manifest', 'source', 'sha256', 'bytes']})
                else:
                    models[model.destination] = model
            packs = []
            classes = {node.class_type for node in wf.runtime_nodes().values()}
            for pack in resolve_node_packs(classes, lockfile_path=lockpath):
                row = node_locks.get(pack.name)
                revision = getattr(row, 'git_commit_sha', None)
                repo = getattr(row, 'url', None)
                if not repo or not revision or not re.fullmatch('[0-9a-f]{40}', revision):
                    unlocked.append({'kind': 'node_pack', 'name': pack.name, 'repository': pack.repo,
                                     'classes': sorted(pack.classes & classes), 'missing': ['locked_repository_and_sha']})
                else:
                    packs.append({'name': pack.name, 'repository': repo, 'revision': revision,
                                  'classes': sorted(pack.classes & classes)})
            modes.append({'mode': mode, 'workflow': wf, 'models': list(models.values()),
                          'node_packs': packs, 'unlocked': unlocked})
        manifests.append({'id': entry.id, 'modes': modes, 'provider_only': not modes})
    return manifests


def _pack_state(target: ManagedAssetTarget, pack: dict[str, Any]) -> str:
    _validate_pack(pack)
    path = _safe_path(target.custom_nodes_root, target.custom_nodes_root / _safe_relative(pack['name']))
    if not path.exists():
        return 'missing'
    if not path.is_dir() or (path / '.git').is_symlink() or not (path / '.git').is_dir():
        raise IntegrityError(f"node pack {pack['name']} is not a plain Git checkout")
    def git(*args):
        return subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(path), *args],
                              capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    try:
        if (git('rev-parse', 'HEAD') != pack['revision']
                or git('config', '--get', 'remote.origin.url') != pack['repository']
                or git('status', '--porcelain', '--untracked-files=all')):
            return 'mismatch'
    except (subprocess.SubprocessError, OSError) as exc:
        raise IntegrityError(f"Cannot verify node pack {pack['name']}") from exc
    return 'verified'


def _validate_pack(pack: dict[str, Any]) -> None:
    name = _safe_relative(pack['name'])
    source = urlsplit(pack['repository'])
    if (len(name.parts) != 1 or not re.fullmatch('[0-9a-f]{40}', pack['revision'])
            or source.scheme != 'https' or not source.hostname
            or source.username is not None or source.password is not None or source.fragment):
        raise IntegrityError(f"node pack {pack['name']} requires a credential-free HTTPS repository and full locked SHA")


def _install_pack(target: ManagedAssetTarget, pack: dict[str, Any], *, deadline: float) -> None:
    _validate_pack(pack)
    _safe_path(target.custom_nodes_root, target.custom_nodes_root / _safe_relative(pack['name']))
    lockpath = _safe_path(target.root, target.root / '.astrid-engine/aligned-custom-nodes.lock')
    _root_and_destination(str(target.custom_nodes_root), PurePosixPath('.vibecomfy-install-state/check'))
    if target.vibecomfy_root is None:
        raise IntegrityError('managed target has no verified VibeComfy source')
    # Run the existing installer inside the target environment so its pip
    # protection checks observe the target's actual installed distributions.
    code = '''import json, sys
from dataclasses import asdict
from pathlib import Path
from vibecomfy.node_packs import install_pack
from vibecomfy.security.gate import requesting_provenance
pack = json.loads(sys.argv[1])
token = requesting_provenance.set('user_confirmed')
try:
    result = install_pack(name=pack['name'], repo=pack['repository'],
        install_root=Path(sys.argv[2]), lockfile_path=Path(sys.argv[3]),
        checkout_ref=pack['revision'], expected_commit=pack['revision'],
        cm_cli_resolver=lambda *_: None)
finally:
    requesting_provenance.reset(token)
print(json.dumps(asdict(result)))
'''
    try:
        child = subprocess.run([target.python, '-c', code, json.dumps(pack), str(target.custom_nodes_root), str(lockpath)],
            cwd=target.root, env={**os.environ, 'PYTHONPATH': str(target.vibecomfy_root),
                                  'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'},
            capture_output=True, text=True, check=True, timeout=max(0.001, min(600, deadline - time.monotonic())))
        result = json.loads(child.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise AlignmentError(f"Failed to install node pack {pack['name']} at locked SHA {pack['revision']}") from exc
    if result['status'] not in {'installed', 'refreshed'} or result['git_commit_sha'] != pack['revision']:
        raise AlignmentError(f"Failed to install node pack {pack['name']} at locked SHA {pack['revision']}")
    if _pack_state(target, pack) != 'verified':
        raise IntegrityError(f"Installed node pack {pack['name']} failed repository/SHA verification")


def inspect_assets(manifests, target: ManagedAssetTarget, *, observed=None, deadline=None) -> dict[str, Any]:
    from .engine_runner import _disk_anchor
    deadline = deadline or time.monotonic() + 3600
    unique_missing = {}
    entries = []
    for manifest in manifests:
        modes = []
        for mode in manifest['modes']:
            model_rows, pack_rows = [], []
            for model in mode['models']:
                relative, url = _validate_model(model)
                _, path = _root_and_destination(str(target.models_root), relative)
                _safe_path(target.models_root, path)
                state = 'missing' if not path.exists() else (
                    'verified' if _matches(path, model, deadline=deadline) else 'mismatch')
                if state == 'missing':
                    unique_missing[model.destination] = model.bytes
                model_rows.append({**asdict(model), 'source_url': url, 'target_path': str(path),
                                   'target_folder': str(path.parent), 'status': state})
            for pack in mode['node_packs']:
                pack_rows.append({**pack, 'status': _pack_state(target, pack)})
            nodes, models = (workflow_asset_gaps(mode['workflow'], observed)
                             if observed is not None else ([], []))
            ready = (observed is not None and not mode['unlocked'] and not nodes and not models
                     and all(row['status'] == 'verified' for row in [*model_rows, *pack_rows]))
            modes.append({'mode': mode['mode'], 'models': model_rows, 'node_packs': pack_rows,
                          'unlocked': mode['unlocked'], 'missing_nodes': nodes,
                          'missing_model_choices': models, 'status': 'ready' if ready else 'not_ready'})
        entries.append({'id': manifest['id'], 'provider_only': manifest['provider_only'], 'modes': modes,
                        'align_command': shlex.join(align_argv(manifest['id'])),
                        'status': 'provider_only' if manifest['provider_only'] else (
                            'ready' if all(mode['status'] == 'ready' for mode in modes) else 'not_ready')})
    required = sum(unique_missing.values())
    _, _, _, free = _disk_anchor(str(target.models_root))
    return {'target': {'root': str(target.root), 'url': target.url,
                       'models_lock_sha256': target.models_lock_sha256},
            'entries': entries, 'total_missing_bytes': required,
            'unlocked_bytes': 'unknown' if any(mode['unlocked'] for entry in entries for mode in entry['modes']) else 0,
            'disk_check': {'required_bytes': required, 'free_bytes': free,
                           'status': 'ok' if free >= required else 'blocked',
                           'basis': 'missing_locked_models; additional node/dependency space is unquantified'},
            'object_info': 'unknown' if observed is None else 'observed',
            'status': 'ready' if all(entry['status'] in {'ready', 'provider_only'} for entry in entries) else 'not_ready'}


def align(*, entry_id=None, all_entries=False, apply=False, consent=(), target=None, manifests=None,
              opener=None, installer=None, deadline_seconds=3600) -> dict[str, Any]:
    # Check consent before target selection, probing, network or writes.
    if apply and (not isinstance(consent, (tuple, list, set, frozenset)) or 'user_env' not in consent):
        raise AlignmentError('Applying locked assets requires explicit consent; run ' + shlex.join(align_argv(entry_id)))
    if entry_id is not None and all_entries:
        raise AlignmentError('Choose --entry ID or --all')
    manifests = catalog_manifests(entry_id) if manifests is None else manifests
    target = selected_target() if target is None else target
    deadline = time.monotonic() + deadline_seconds
    if apply:
        with target.install_lock(deadline=deadline):
            return _align_selected(manifests, target, entry_id=entry_id, apply=True,
                opener=opener, installer=installer, deadline=deadline)
    return _align_selected(manifests, target, entry_id=entry_id, apply=False, deadline=deadline)


def _align_selected(manifests, target, *, entry_id, apply, deadline, opener=None, installer=None):
    observed = target.cached_object_info()
    report = inspect_assets(manifests, target, observed=observed, deadline=deadline)
    report['align_command'] = shlex.join(align_argv(entry_id))
    report['applied'] = False
    if observed is not None:
        report['object_info'] = 'cached'
    if not apply:
        return report
    # Refuse all corrupt/existing assets before any downloads or node installs.
    for entry in report['entries']:
        for mode in entry['modes']:
            for row in [*mode['models'], *mode['node_packs']]:
                if row['status'] == 'mismatch':
                    raise IntegrityError(f"Existing asset {row.get('destination', row.get('name'))} differs from its lock; restore verified bytes before running astrid generate align")
    if report['disk_check']['status'] == 'blocked':
        raise AlignmentError(f"Insufficient disk for missing locked models: need {report['total_missing_bytes']} bytes, free {report['disk_check']['free_bytes']}; free disk and retry {report['align_command']}")
    target.verify_installation(deadline=deadline)
    actions, seen_models, seen_packs = [], set(), set()
    for manifest in manifests:
        for mode in manifest['modes']:
            for model in mode['models']:
                if model.destination not in seen_models:
                    try:
                        result = download_model(model, models_root=str(target.models_root), deadline=deadline, opener=opener)
                    except EngineError as exc:
                        raise AlignmentError(f'Model {model.destination}: {exc}; retry {report["align_command"]}') from exc
                    actions.append(asdict(result))
                    seen_models.add(model.destination)
            for pack in mode['node_packs']:
                if pack['name'] not in seen_packs:
                    if _pack_state(target, pack) != 'verified':
                        if installer is None:
                            _install_pack(target, pack, deadline=deadline)
                        else:
                            installer(target, pack)
                    seen_packs.add(pack['name'])
    try:
        observed = target.object_info()
    except (ValueError, OSError, EngineError) as exc:
        report = inspect_assets(manifests, target, deadline=deadline)
        report['probe_error'] = 'Selected target object_info is unavailable; start the installed engine and retry ' + shlex.join(align_argv(entry_id))
    else:
        target.cache_object_info(observed)
        report = inspect_assets(manifests, target, observed=observed, deadline=deadline)
    report.update(applied=True, actions=actions, align_command=shlex.join(align_argv(entry_id)))
    if any(mode['missing_nodes'] for entry in report['entries'] for mode in entry['modes']):
        report['next_action'] = 'Restart the selected managed ComfyUI to load installed node packs, then retry ' + report['align_command']
    if report['status'] == 'ready':
        try:
            target.verify_readiness(deadline=deadline)
        except EngineError as exc:
            report['status'] = 'not_ready'
            report['managed_readiness_error'] = exc.code
            report['next_action'] = 'Managed engine admission still fails; repair the installed engine before retrying ' + report['align_command']
    return report


def main(args) -> int:
    try:
        report = align(entry_id=args.entry, all_entries=args.all, apply=args.apply, consent=args.consent)
    except (EngineError, ValueError, OSError) as exc:
        message = str(exc)
        if 'astrid generate align' not in message:
            message += '; retry ' + shlex.join(align_argv(args.entry))
        payload = {'error': {'code': getattr(exc, 'code', 'asset_alignment_failed'), 'message': message}}
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"Managed target: {report['target']['root']} ({report['status']})")
        for entry in report['entries']:
            print(f"{entry['id']}: {entry['status']}")
            for mode in entry['modes']:
                for row in mode['models']:
                    print(f"  {mode['mode']}: {row['destination']} {row['status']} ({row['bytes']} bytes, SHA-256 {row['sha256']}, {row['source_url']}, folder {row['target_folder']})")
                for row in mode['node_packs']:
                    print(f"  {mode['mode']}: {row['name']} {row['repository']} @ {row['revision']}: {row['status']}")
                for gap in mode['unlocked']:
                    print(f"  {mode['mode']}: unlocked {json.dumps(gap, sort_keys=True)}")
                for gap in [*mode['missing_nodes'], *mode['missing_model_choices']]:
                    print(f'  Missing from object_info: {gap}')
        print(f"Missing locked bytes: {report['total_missing_bytes']}; disk: {report['disk_check']['status']} ({report['disk_check']['free_bytes']} free); object_info: {report['object_info']}")
        print('Apply: ' + report['align_command'])
        if report.get('probe_error') or report.get('next_action'):
            print(report.get('probe_error') or report.get('next_action'))
    return 0 if not args.apply or report['status'] == 'ready' else 1
