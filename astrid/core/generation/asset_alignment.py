"""Inspect and align catalog assets on an existing managed Comfy target.

Inspection is offline. A missing target-specific schema cache is unknown, never
proof of readiness. Apply requires fresh user_env consent and re-probes the same
endpoint; it never provisions, starts or restarts an engine.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
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
from .engine_contracts import (
    DeadlineExceeded,
    DownloadError,
    DownloadResult,
    EngineError,
    EngineRecipe,
    IntegrityError,
    ModelSpec,
    UnsafePathError,
    effective_models_root,
)
from .engine_recipe import load_model_lock, load_recipe, load_recipe_index
from .model_downloader import _matches, _root_and_destination, _safe_relative, _validate_model


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
    recipe: EngineRecipe | None = None

    def object_info(self) -> dict[str, Any]:
        from .backends.comfy_target import ComfyTarget, ComfyTargetClient
        return ComfyTargetClient(ComfyTarget(self.url, 'managed')).object_info()

    def cached_object_info(self) -> dict[str, Any] | None:
        from vibecomfy.schema.cache import (
            CACHE_METADATA_KEY,
            load_object_info_cache,
            object_info_payload,
            validate_object_info_cache,
        )
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
        from .engine_runner import (
            _parse_receipt,
            _Runner,
            plan_from_receipt,
            plan_install,
            require_current_receipt,
        )

        raw = json.loads(_safe_path(self.root, self.root / '.astrid-engine/install.json').read_bytes())
        recipe = load_recipe(raw['engine'], raw['platform'])
        # The receipt records the model directory the install bound (a volume cache, or <root>/models).
        receipt = _parse_receipt(raw, plan_install(recipe, root=str(self.root), placement='local'))
        plan = plan_from_receipt(recipe, receipt, root=str(self.root), placement='local')
        require_current_receipt(receipt, plan, recipe)
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

    def install_lock(self, *, deadline: float):
        from .engine_platforms import platform_adapter
        raw = json.loads(_safe_path(self.root, self.root / '.astrid-engine/install.json').read_bytes())
        recipe = load_recipe(raw['engine'], raw['platform'])
        return platform_adapter(recipe.platform).local_install_lock(str(self.root), deadline=deadline)

    def verify_readiness(self, *, deadline: float, entry_id: str | None = None) -> None:
        from .engine_local_endpoint import local_engine_endpoint
        installed = local_engine_endpoint('comfyui', root=str(self.root),
            deadline_seconds=max(0.001, min(120, deadline - time.monotonic())),
            **({'entry_id': entry_id} if entry_id is not None else {}))
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


class _ResumingClient:
    """httpx-style client for VibeComfy fetch that keeps a verified-later `.part` prefix.

    fetch streams to its own temp file and deletes it on failure, so resume lives here: the
    streamed bytes are mirrored into `part`, and a later run asks for `Range: bytes=<prefix>-`
    and replays the prefix ahead of the new bytes. A server that ignores Range restarts at 0.
    """

    def __init__(self, inner, part: Path, deadline: float | None, progress=None):
        self.inner, self.part, self.deadline, self.progress = inner, part, deadline, progress
        self.resumed_from = 0

    @contextlib.contextmanager
    def stream(self, method, url, **kwargs):
        prefix = self.part.stat().st_size if self.part.exists() else 0
        headers = dict(kwargs.pop('headers', None) or {})
        if prefix:
            headers['Range'] = f'bytes={prefix}-'
        with self.inner.stream(method, url, headers=headers, **kwargs) as response:
            if prefix and response.status_code == 206:
                start = re.match(r'bytes (\d+)-', str(response.headers.get('Content-Range', '')))
                if start is None or int(start.group(1)) != prefix:
                    raise DownloadError('server answered a resume request with a different range')
            else:
                prefix = 0
            self.resumed_from = prefix
            yield _SplicedResponse(response, self, prefix)


class _SplicedResponse:
    def __init__(self, response, client: _ResumingClient, prefix: int):
        self._response, self._client, self._prefix = response, client, prefix
        self.url = getattr(response, 'url', None)

    @property
    def status_code(self) -> int:
        return 200 if 200 <= self._response.status_code < 300 else self._response.status_code

    def iter_bytes(self):
        client = self._client
        if self._prefix:
            with client.part.open('rb') as handle:
                for chunk in iter(lambda: handle.read(8 << 20), b''):
                    yield chunk
        done = self._prefix
        with client.part.open('ab' if self._prefix else 'wb') as handle:
            for chunk in self._response.iter_bytes():
                if client.deadline is not None and time.monotonic() >= client.deadline:
                    raise DeadlineExceeded('model download deadline exceeded')
                handle.write(chunk)
                handle.flush()
                done += len(chunk)
                if client.progress is not None:
                    client.progress(done)
                yield chunk


def fetch_verified(entry: dict[str, Any], path: Path, *, root: Path, part: Path, label: str,
                   client=None, deadline: float | None = None, progress=None) -> int:
    """Fetch one file through VibeComfy fetch into `path`, resuming from `part`, then check it against `entry`.

    `part` keeps an interrupted transfer's bytes for the next call, which asks for the rest with a Range
    request. A file that fails its size or SHA-256 check is removed together with `part`. Returns the
    byte offset this transfer resumed from.

    fetch publishes before it verifies, so a failed check unlinks what fetch just published.
    """
    from vibecomfy import fetch

    if part.exists() and part.stat().st_size >= entry['size_bytes']:
        part.unlink()  # nothing left to resume from
    owned = None
    if client is None:
        # The operating system's trust store, so antivirus and company proxies that inspect HTTPS work.
        from astrid.core.util.http import trusted_httpx_client
        client = owned = trusted_httpx_client()
    resuming = _ResumingClient(client, part, deadline, progress)
    error = None
    try:
        fetch.download(entry, root=root, client=resuming, quiet=True)
    except Exception as exc:  # noqa: BLE001 - reported through the raised error or fallback; diagnostic at debug, never a user-facing traceback
        # Keep failures for the no-published-file error below; a published file is still
        # force-verified before it can be accepted.
        logging.getLogger(__name__).debug('fetch failed for %s', label, exc_info=True)
        error = exc
    finally:
        if owned is not None:
            owned.close()
    # A caller may pre-claim `path` as an empty placeholder: only bytes fetch actually published count here.
    published = path.exists() and (error is None or path.stat().st_size > 0)
    if published:
        try:
            # fetch published first and left its source receipt over the verification sidecar
            # model_root reads; force-verifying checks size and SHA-256 and rewrites that sidecar.
            fetch.verify(entry, path, root=root, force=True)
        except Exception as exc:
            path.unlink()
            part.unlink(missing_ok=True)
            raise IntegrityError(f'{label}: SHA-256 mismatch against the lock ({exc}); refused to publish') from exc
    else:
        kept = '; prefix retained for resume' if part.exists() else ''
        if isinstance(error, DownloadError):
            raise error
        raise DownloadError(f'{label}: download failed ({error}){kept}') from error
    part.unlink(missing_ok=True)
    return resuming.resumed_from


def _fetch_model(model: ModelSpec, models_root: Path, *, deadline: float, client=None) -> DownloadResult:
    """Fetch one locked model through VibeComfy fetch, then check it against the lock."""
    relative, url = _validate_model(model)
    _, path = _root_and_destination(str(models_root), relative)
    _safe_path(models_root, path)
    if path.exists():
        if _matches(path, model, deadline=deadline):
            return DownloadResult(model=model.name, path=str(path), action='skipped', bytes=model.bytes,
                                  sha256=model.sha256, verified=True, resumed_from_bytes=0)
        raise IntegrityError(f'Existing asset {model.destination} differs from its lock; restore verified bytes')
    part = path.with_name(path.name + '.part')
    _safe_path(models_root, part)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {'name': path.name, 'subdir': relative.parent.as_posix(), 'url': url,
             'sha256': model.sha256, 'size_bytes': model.bytes}
    resumed = fetch_verified(entry, path, root=models_root, part=part, label=f'locked model {model.destination}',
                             client=client, deadline=deadline)
    return DownloadResult(model=model.name, path=str(path), action='resumed' if resumed else 'downloaded',
                          bytes=model.bytes, sha256=model.sha256, verified=True, resumed_from_bytes=resumed)


def selected_target() -> ManagedAssetTarget:
    from vibecomfy.schema.cache import object_info_cache_path

    from .engine_local_endpoint import recorded_engine_root
    from .engine_runner import (
        _parse_receipt,
        plan_from_receipt,
        plan_install,
        require_current_receipt,
    )

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
    plan = plan_from_receipt(recipe, receipt, root=str(root), placement='local')
    if receipt.engine != 'comfyui' or receipt.status != 'installed':
        raise IntegrityError('managed target needs an installed ComfyUI receipt before running astrid generate align')
    require_current_receipt(receipt, plan, recipe)
    comfy = root / next(source.relative_path for source in recipe.sources if source.name == 'comfyui')
    url = f'http://127.0.0.1:{recipe.start.port}'
    models = Path(effective_models_root(str(root), receipt.models_root))
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
                              load_model_lock(recipe.models_lock).models, recipe)


def _locked_models() -> dict[str, ModelSpec]:
    """Load only digest-verified packaged locks; do not enrich from the network."""
    result = {}
    for indexed in load_recipe_index().entries:
        recipe = load_recipe(indexed.engine, indexed.platform)
        for model in load_model_lock(recipe.models_lock).models:
            previous = result.get(model.destination)
            if previous is not None and not _same_artifact_identity(previous, model):
                raise IntegrityError(f'conflicting packaged model locks: {model.destination}')
            result[model.destination] = model
    return result


def _same_artifact_identity(left: ModelSpec, right: ModelSpec) -> bool:
    """Compare locked artifact facts, excluding the per-lock display name."""
    return (
        left.repository,
        left.revision,
        left.path,
        left.url,
        left.destination,
        left.bytes,
        left.sha256,
    ) == (
        right.repository,
        right.revision,
        right.path,
        right.url,
        right.destination,
        right.bytes,
        right.sha256,
    )


def catalog_manifests(entry_id: str | None = None) -> list[dict[str, Any]]:
    from vibecomfy.model_assets import _referenced_model_values, _subdir_for_model_reference
    from vibecomfy.node_approvals import reviewed_approvals
    from vibecomfy.node_packs import read_lockfile, resolve_node_packs
    from vibecomfy.node_packs._lockfile import resolve_lockfile_path

    from astrid.core.model_catalog.registry import ModelRegistry

    from .backends.vibecomfy import _load_pinned_catalog_workflow

    locked = _locked_models()
    lockpath = resolve_lockfile_path()
    node_locks = {row.name: row for row in read_lockfile(lockpath)}
    entries = ModelRegistry.load_default().list_all(include_closed=True)
    if entry_id is not None:
        entries = [entry for entry in entries if entry.id == entry_id]
        if not entries:
            from .asset_gaps import declared_model_specs
            from .entries import _load_entry_workflow, find_entry
            entry = find_entry(entry_id)
            if entry is None or entry.get('source') not in {'ready-template', 'user-entry'}:
                raise AlignmentError(f'Unknown pinned entry {entry_id}; astrid generate align requires a catalog, ready-template or saved user entry ID')
            workflow = _load_entry_workflow(entry)
            classes = {node.class_type for node in workflow.runtime_nodes().values()}
            packs, unlocked = [], []
            if entry['source'] == 'user-entry':
                from vibecomfy.custom_node_refs import workflow_lock_entries
                from vibecomfy.entries import entries_dir, load_entry
                from vibecomfy.node_recipes import recipe_sha256

                from .node_approvals import approval_matches, read_entry_approvals

                # Approvals belong beside the saved manifest, not inside a
                # versioned bundle or a similarly named contributed entry.
                directory = entries_dir() / entry_id
                approvals = []
                if (directory / 'entry.json').is_file():
                    saved = load_entry(directory)
                    if saved['id'] == entry_id and saved['bundle'] == entry['bundle']:
                        approvals = read_entry_approvals(directory)

                # User entries carry their own identities. Never replace a
                # missing or different pin with a same-named shipped pack.
                by_name = {}
                declared = set()
                for row in workflow_lock_entries(workflow):
                    declared.update({row.name, row.slug or row.name})
                    revision, repo = row.git_commit_sha or row.commit, row.url
                    if not repo or not revision or not re.fullmatch('[0-9a-f]{40}', revision):
                        unlocked.append({'kind': 'node_pack', 'name': row.name,
                                         'missing': ['locked_repository_and_sha']})
                        continue
                    pack = {'name': row.name, 'repository': repo, 'revision': revision,
                            'classes': sorted(set(row.class_set) & classes)}
                    _validate_pack(pack)
                    recipe = {'requirements_txt': row.requirements_txt, 'pip_no_deps': row.pip_no_deps}
                    pack.update(requirements_txt=row.requirements_txt, pip_no_deps=list(row.pip_no_deps))
                    recipe_hash = recipe_sha256(recipe)
                    node = {'repo': repo, 'commit': revision, **recipe}
                    if not reviewed_approvals([node], placement_class='local'):
                        approved = any(approval_matches(record, node, placement_class='local')
                                       for record in approvals)
                        if not approved:
                            pack['requires_code_consent'] = True
                        pack['code_approvals'] = [record for record in approvals
                            if approval_matches(record, node, placement_class='local')]
                    prior = by_name.get(row.name)
                    if prior is not None:
                        if prior[0] != recipe_hash or (prior[1]['repository'], prior[1]['revision']) != (repo, revision):
                            raise AlignmentError(f'Conflicting bundle node pack pins or recipes for {row.name}')
                        pack['classes'] = sorted(set(prior[1]['classes']) | set(pack['classes']))
                    by_name[row.name] = (recipe_hash, pack)
                for name in sorted(set(workflow.requirements.custom_nodes) - declared):
                    unlocked.append({'kind': 'node_pack', 'name': name,
                                     'missing': ['locked_repository_and_sha']})
                packs = [pack for _, pack in by_name.values()]
                incomplete = []
                models = list(declared_model_specs(workflow.metadata.get('model_assets', []),
                                                   incomplete=incomplete))
                retry = shlex.join(['astrid', 'generate', 'align', '--entry', entry_id, '--json'])
                for record in incomplete:
                    name = record['name']
                    subdir = record['subdir']
                    destination = (f'{subdir}/{name}' if isinstance(subdir, str) and subdir not in ('', '.')
                                   and name else name)
                    label = destination or 'the unnamed model declaration'
                    unlocked.append({
                        'kind': 'model', 'code': 'model_lock_incomplete', 'severity': 'error',
                        'name': name, 'destination': destination, 'missing': record['missing'],
                        'next_action': (
                            f'Obtain the verified HTTPS source, SHA-256 and exact byte size for {label} '
                            'and supply the missing facts in the saved bundle model declaration. '
                            'Complete the declaration through the workflow authoring path. '
                            f'Then run {retry}.'),
                    })
                # Assets belong to the workflow even if its saved generation
                # mode has not been chosen (e.g. an imported audio workflow).
                return [{'id': entry_id, 'provider_only': False, 'modes': [
                    {'mode': mode, 'workflow': workflow, 'models': models,
                     'node_packs': packs, 'unlocked': unlocked}
                    for mode in (entry['modes'] or ['workflow'])]}]
            for pack in resolve_node_packs(classes, lockfile_path=lockpath):
                row = node_locks.get(pack.name)
                revision, repo = getattr(row, 'git_commit_sha', None), getattr(row, 'url', None)
                if not repo or not revision or not re.fullmatch('[0-9a-f]{40}', revision):
                    unlocked.append({'kind': 'node_pack', 'name': pack.name,
                                     'missing': ['locked_repository_and_sha']})
                else:
                    packs.append(_locked_node_pack(row, pack.classes & classes))
            return [{'id': entry_id, 'provider_only': False, 'modes': [
                {'mode': mode, 'workflow': workflow, 'models': list(declared_model_specs(workflow.metadata.get('model_assets', []))),
                 'node_packs': packs, 'unlocked': unlocked} for mode in entry['modes']]}]
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
                    packs.append(_locked_node_pack(row, pack.classes & classes))
            modes.append({'mode': mode, 'workflow': wf, 'models': list(models.values()),
                          'node_packs': packs, 'unlocked': unlocked})
        manifests.append({'id': entry.id, 'modes': modes, 'provider_only': not modes})
    return manifests


def _locked_node_pack(row, classes):
    """Project the complete shipped recipe used by canonical code approval."""
    pack = {'name': row.name, 'repository': row.url, 'revision': row.git_commit_sha or row.commit,
            'classes': sorted(classes)}
    if row.requirements_txt is not None:
        pack['requirements_txt'] = row.requirements_txt
    if row.pip_no_deps:
        pack['pip_no_deps'] = list(row.pip_no_deps)
    return pack


def _pack_recipe_patches(target: ManagedAssetTarget, pack: dict[str, Any]):
    if target.recipe is None:
        return ()
    from .managed_preparation import _recipe_node_patches
    patches = _recipe_node_patches(target.recipe).get(pack['name'], ())
    sources = {source.name: source for source in target.recipe.sources}
    return tuple(patch for patch in patches if sources[patch.source].pin.repository == pack['repository']
                 and sources[patch.source].pin.revision == pack['revision'])


def _pack_state(target: ManagedAssetTarget, pack: dict[str, Any]) -> str:
    _validate_pack(pack)
    path = _safe_path(target.custom_nodes_root, target.custom_nodes_root / _safe_relative(pack['name']))
    if not path.exists():
        return 'missing'
    if not path.is_dir() or (path / '.git').is_symlink() or not (path / '.git').is_dir():
        raise IntegrityError(f"node pack {pack['name']} is not a plain Git checkout")
    def git(*args):
        return subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(path), *args],
                              capture_output=True, text=True, check=True, timeout=15).stdout.rstrip('\n')
    try:
        head = git('rev-parse', 'HEAD')
        if head != pack['revision'] or git('config', '--get', 'remote.origin.url') != pack['repository']:
            return 'mismatch'
        dirty = git('status', '--porcelain', '--untracked-files=all')
        if dirty:
            patches = _pack_recipe_patches(target, pack)
            if not patches:
                return 'mismatch'
            from vibecomfy.node_packs._install import _verified_declared_patches
            from vibecomfy.node_packs._lockfile import DeclaredPatch, LockEntry
            entry = LockEntry(pack['name'], commit=pack['revision'], url=pack['repository'],
                              patches=tuple(DeclaredPatch(p.path, p.sha256_after) for p in patches))
            if not _verified_declared_patches(path, dirty, entry, head):
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
    from .node_approvals import target_approvals
    node = {'repo': pack['repository'], 'commit': pack['revision'],
            'requirements_txt': pack.get('requirements_txt'), 'pip_no_deps': pack.get('pip_no_deps', [])}
    approvals = target_approvals([node], pack.get('code_approvals', []), placement_class='local')
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
from vibecomfy.node_packs import restore_pack
from vibecomfy.node_packs._lockfile import DeclaredPatch, LockEntry
pack = json.loads(sys.argv[1])
entry = LockEntry(name=pack['name'], url=pack['repository'], commit=pack['revision'],
    requirements_txt=pack.get('requirements_txt'), pip_no_deps=tuple(pack.get('pip_no_deps', [])),
    patches=tuple(DeclaredPatch(**patch) for patch in json.loads(sys.argv[4])))
result = restore_pack(entry, install_root=Path(sys.argv[2]), lockfile_path=Path(sys.argv[3]),
    approvals=json.loads(sys.argv[5]), placement_class='local')
print(json.dumps(asdict(result)))
'''
    try:
        child = subprocess.run([target.python, '-c', code, json.dumps(pack), str(target.custom_nodes_root), str(lockpath),
                                json.dumps([{'path': p.path, 'sha256': p.sha256_after}
                                            for p in _pack_recipe_patches(target, pack)]), json.dumps(approvals)],
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
    code_next_action = None
    model_next_action = None
    for manifest in manifests:
        modes = []
        for mode in manifest['modes']:
            if model_next_action is None:
                model_next_action = next((gap['next_action'] for gap in mode['unlocked']
                                          if gap.get('code') == 'model_lock_incomplete'), None)
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
            code_blocked = False
            for pack in mode['node_packs']:
                state = _pack_state(target, pack)
                pack_rows.append({**pack, 'status': state})
                problem = _bundle_alignment_code_problem(pack, state)
                if problem:
                    code_blocked = True
                    if code_next_action is None:
                        code_next_action = problem + shlex.join(align_argv(manifest['id']))
            nodes, models = (workflow_asset_gaps(mode['workflow'], observed)
                             if observed is not None else ([], []))
            ready = (observed is not None and not code_blocked and not mode['unlocked'] and not nodes and not models
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
            'status': 'ready' if all(entry['status'] in {'ready', 'provider_only'} for entry in entries) else 'not_ready',
            **({'next_action': code_next_action or model_next_action}
               if code_next_action or model_next_action else {})}


def align(*, entry_id=None, all_entries=False, apply=False, consent=(), target=None, manifests=None,
              client=None, installer=None, deadline_seconds=3600) -> dict[str, Any]:
    # Check consent before target selection, probing, network or writes.
    if apply and (not isinstance(consent, (tuple, list, set, frozenset)) or 'user_env' not in consent):
        raise AlignmentError('Applying locked assets requires explicit consent; run ' + shlex.join(align_argv(entry_id)))
    if entry_id is not None and all_entries:
        raise AlignmentError('Choose --entry ID or --all')
    manifests = catalog_manifests(entry_id) if manifests is None else manifests
    packs = [pack for manifest in manifests for mode in manifest['modes'] for pack in mode['node_packs']]
    if apply and packs:
        from .entries import find_entry
        from .node_approvals import authorize_nodes, approval_record, reviewed_approvals
        manifests = [{**manifest, 'modes': [{**mode, 'node_packs': [dict(pack) for pack in mode['node_packs']]}
                                         for mode in manifest['modes']]} for manifest in manifests]
        packs = [pack for manifest in manifests for mode in manifest['modes'] for pack in mode['node_packs']]
        nodes = list({(pack['repository'], pack['revision'], pack.get('requirements_txt'),
                       tuple(pack.get('pip_no_deps', []))): {
                           'repo': pack['repository'], 'commit': pack['revision'],
                           'requirements_txt': pack.get('requirements_txt'), 'pip_no_deps': pack.get('pip_no_deps', [])}
                      for pack in packs}.values())
        approvals = authorize_nodes(nodes, placement_class='local', consent=consent,
            entry=find_entry(entry_id) if entry_id else None,
            retry_argv=align_argv(entry_id), persist=True)
        reviewed = reviewed_approvals(nodes, placement_class='local')
        for pack in packs:
            node = {'repo': pack['repository'], 'commit': pack['revision'],
                    'requirements_txt': pack.get('requirements_txt'), 'pip_no_deps': pack.get('pip_no_deps', [])}
            current = approval_record(node, placement_class='local')
            if current in reviewed:
                # Review already covers this exact source and recipe. Keep
                # catalog declarations free of entry-owned consent records;
                # _install_pack rechecks the canonical authority at the target.
                pack.pop('code_approvals', None)
            else:
                pack['code_approvals'] = [row for row in approvals if row == current]
            pack.pop('requires_code_consent', None)
    target = selected_target() if target is None else target
    deadline = time.monotonic() + deadline_seconds
    if apply:
        with target.install_lock(deadline=deadline):
            return _align_selected(manifests, target, entry_id=entry_id, apply=True,
                client=client, installer=installer, deadline=deadline)
    return _align_selected(manifests, target, entry_id=entry_id, apply=False, deadline=deadline)


def _bundle_alignment_code_problem(pack, state):
    if pack.get('requires_code_consent'):
        return (
            f"Node pack {pack['name']} at {pack['revision']} has no matching shipped pin or local code approval; "
            "user_env consent does not authorize installing new node code. "
            "Have the user approve that exact repository, commit and recipe for the managed target, then retry ")
    return None


def _check_bundle_alignment_code(pack, state, retry):
    if problem := _bundle_alignment_code_problem(pack, state):
        raise AlignmentError(problem + retry)


def _align_selected(manifests, target, *, entry_id, apply, deadline, client=None, installer=None):
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
                _check_bundle_alignment_code(row, row['status'], report['align_command'])
    if any(gap.get('code') == 'model_lock_incomplete'
           for entry in report['entries'] for mode in entry['modes']
           for gap in mode.get('unlocked', [])):
        # Keep the typed issue and repair instruction, and make no asset writes
        # while the selected saved entry lacks an authoritative model lock.
        return report
    if report['disk_check']['status'] == 'blocked':
        raise AlignmentError(f"Insufficient disk for missing locked models: need {report['total_missing_bytes']} bytes, free {report['disk_check']['free_bytes']}; free disk and retry {report['align_command']}")
    target.verify_installation(deadline=deadline)
    actions, seen_models, seen_packs = [], set(), set()
    for manifest in manifests:
        for mode in manifest['modes']:
            for model in mode['models']:
                if model.destination not in seen_models:
                    try:
                        result = _fetch_model(model, target.models_root, deadline=deadline, client=client)
                    except EngineError as exc:
                        raise AlignmentError(f'Model {model.destination}: {exc}; retry {report["align_command"]}') from exc
                    actions.append(asdict(result))
                    seen_models.add(model.destination)
            for pack in mode['node_packs']:
                if pack['name'] not in seen_packs:
                    state = _pack_state(target, pack)
                    _check_bundle_alignment_code(pack, state, report['align_command'])
                    if state != 'verified' or pack.get('requirements_txt') or pack.get('pip_no_deps'):
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
    if not report.get('next_action') and any(mode['missing_nodes'] for entry in report['entries'] for mode in entry['modes']):
        report['next_action'] = 'Restart the selected managed ComfyUI to load installed node packs, then retry ' + report['align_command']
    if report['status'] == 'ready':
        try:
            target.verify_readiness(deadline=deadline, entry_id=entry_id)
        except EngineError as exc:
            report['status'] = 'not_ready'
            report['managed_readiness_error'] = exc.code
            report['next_action'] = 'Managed engine admission still fails; repair the installed engine before retrying ' + report['align_command']
    return report


def main(args) -> int:
    from .preflight import GenerationReadinessError
    try:
        report = align(entry_id=args.entry, all_entries=args.all, apply=args.apply, consent=args.consent)
    except GenerationReadinessError:
        # The CLI owns the envelope: keep the human warning and token retry.
        raise
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
