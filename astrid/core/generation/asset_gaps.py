"""Shared read-only workflow asset diagnostics, before canonical approval."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


def declared_model_specs(assets: Any, *, require_complete: bool = True):
    """Project canonical workflow declarations into the existing downloader type.

    Incomplete declarations grant no custody authority. Inventory composition
    may ignore them; provisioning a selected entry must report them instead.
    """
    from .engine_contracts import EngineError, IntegrityError, ModelLockValidationError, ModelSpec
    from .model_downloader import _safe_relative, _validate_model

    if not isinstance(assets, (list, tuple)):
        raise ModelLockValidationError('entry model_assets must be a list')
    models = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ModelLockValidationError('entry model_assets must contain objects')
        missing = [key for key in ('name', 'url', 'sha256', 'size_bytes')
                   if asset.get(key) is None or asset.get(key) == '']
        if missing:
            if require_complete:
                raise ModelLockValidationError(
                    f"Declared model {asset.get('name', '?')} is unavailable: missing {', '.join(missing)}")
            continue
        try:
            name = _safe_relative(asset['name'])
            subdir = asset.get('subdir', '')
            if not isinstance(subdir, str):
                raise ModelLockValidationError('declared model subdir must be a string')
            destination = f'{subdir}/{name}' if subdir not in ('', '.') else str(name)
            model = ModelSpec(asset['name'], None, None, None, asset['url'], destination,
                              asset['size_bytes'], asset['sha256'])
            _validate_model(model)
        except EngineError:
            if require_complete:
                raise
            # A non-provisionable, unselected declaration grants no authority.
            # The exact-root verifier will still reject any unlisted file.
            continue
        previous = models.get(destination)
        if previous is not None and previous != model:
            raise IntegrityError(f'conflicting entry model declarations: {destination}')
        models[destination] = model
    return tuple(models.values())


def align_argv(entry: str | None = None) -> list[str]:
    return ['astrid', 'generate', 'align', '--apply', '--consent', 'user_env',
            *(['--entry', entry] if entry else ['--all'])]


def attached_model_readiness(entry: str | None, placements, *, target=None) -> dict[str, Any]:
    """Readiness for an attached ComfyUI: the user places the file; Astrid installs nothing there."""
    issues, fixes = [], []
    for placement in placements:
        file, role, folder = placement['file'], placement.get('role'), placement.get('folder')
        where = (f"in {folder} on your ComfyUI" if folder
                 else f"in the models folder for {role} on your ComfyUI" if role else "on your ComfyUI")
        label = f'{file} (role {role})' if role else file
        if placement.get('matches'):
            action = f"Keep exactly one of {file} in {folder or 'the models folder for ' + str(role)} on your ComfyUI and remove the others, then retry."
            message = f'Ambiguous model for role {role}: more than one of {file} is present. {action}'
        else:
            action = f"Put {file} {where}, then retry."
            message = f'Missing model {label}. {action}'
        issues.append({'code': 'missing_model', 'severity': 'error',
                       'message': message,
                       'details': {'model': file, 'role': role, 'folder': folder}})
        fixes.append({'scope': 'attached_target', 'details': {'action': action}})
    return {'entry': {'id': entry, 'engine': 'comfyui'}, 'target': target,
            'status': 'blocked', 'issues': issues, 'fixes': fixes}


def _is_comfyui_core_repo(repo: str) -> bool:
    """A pack whose repository is ComfyUI itself ships with ComfyUI, not as a custom node."""
    lowered = repo.lower().rstrip('/')
    return 'github.com/' in lowered and lowered.endswith(('/comfyui', '/comfyui.git'))


def attached_asset_readiness(entry: str | None, *, nodes=(), models=(), lockfile_path=None, target=None) -> dict[str, Any]:
    """Readiness for an attached ComfyUI: the person fixes it on their ComfyUI; Astrid installs and restarts nothing there.

    A node no locked pack provides is part of ComfyUI itself. A node a locked custom pack provides
    names that pack, and its repository when the lock declares a URL.
    """
    from pathlib import Path

    from vibecomfy.node_packs import resolve_node_packs

    lock = Path('custom_nodes.lock') if lockfile_path is None else lockfile_path
    issues, fixes = [], []
    for name in nodes:
        packs = [pack for pack in resolve_node_packs({name}, lockfile_path=lock)
                 if not _is_comfyui_core_repo(pack.repo)]
        if packs:
            pack = packs[0]
            url = pack.repo if pack.repo.startswith(('http://', 'https://', 'git@')) else None
            where = f' ({url})' if url else ''
            action = (f"Install {pack.name}{where} in your ComfyUI's custom_nodes folder, "
                      "then restart ComfyUI yourself and retry.")
            message = f'Your ComfyUI does not have {name}, which comes from the custom node pack {pack.name}. {action}'
            details = {'node': name, 'pack': pack.name, 'repository': url}
        else:
            action = 'Update ComfyUI, then retry.'
            message = (f'Your ComfyUI does not have {name}, which is part of ComfyUI itself; '
                       'update ComfyUI, then retry.')
            details = {'node': name, 'pack': None, 'repository': None}
        issues.append({'code': 'missing_node_pack', 'severity': 'error', 'message': message, 'details': details})
        fixes.append({'scope': 'attached_target', 'details': {'action': action}})
    for name in models:
        action = 'Put the model file in its models folder on your ComfyUI yourself, then retry.'
        issues.append({'code': 'missing_model', 'severity': 'error',
                       'message': f'Missing model {name} on your ComfyUI. {action}',
                       'details': {'model': name}})
        fixes.append({'scope': 'attached_target', 'details': {'action': action}})
    return {'entry': {'id': entry, 'engine': 'comfyui'}, 'target': target,
            'status': 'blocked', 'issues': issues, 'fixes': fixes}


def asset_readiness(entry: str | None, *, nodes=(), models=(), target=None, placements=None) -> dict[str, Any]:
    if placements is not None:
        return attached_model_readiness(entry, placements, target=target)
    argv = align_argv(entry)
    issues = [
        {'code': 'missing_node_pack', 'severity': 'error',
         'message': f'Missing ComfyUI node {name}; install its locked node pack. Run: {shlex.join(argv)}',
         'details': {'node': name}}
        for name in nodes
    ] + [
        {'code': 'missing_model', 'severity': 'error',
         'message': f'Missing model {name}. Run: {shlex.join(argv)}',
         'details': {'model': name}}
        for name in models
    ]
    return {'entry': {'id': entry, 'engine': 'comfyui'}, 'target': target,
            'status': 'blocked', 'issues': issues,
            'fixes': [{'argv': argv, 'scope': 'user_env',
                       'details': {'action': 'Install missing locked assets on the managed target; unlocked assets require a manifest.'}}]}


def workflow_asset_gaps(workflow: Any, observed: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Use the same environment-asset choice scope as local-server preflight."""
    from vibecomfy.schema.validate import _choice_scope

    api = workflow.compile('api')
    nodes = sorted({n['class_type'] for n in api.values()} - observed.keys())
    models = set()
    for node in api.values():
        kind = node['class_type']
        groups = observed.get(kind, {}).get('input', {})
        for field, value in node.get('inputs', {}).items():
            raw = next((groups[group][field] for group in ('required', 'optional')
                        if field in groups.get(group, {})), None)
            choices = raw[0] if isinstance(raw, (list, tuple)) and raw else None
            if (isinstance(choices, (list, tuple)) and isinstance(value, str)
                    and value not in choices and _choice_scope(kind, field, value) == 'environment_asset'):
                models.add(f'{value} ({kind}.{field})')
    return nodes, sorted(models)


def require_workflow_assets(workflow: Any, observed: dict[str, Any], entry: str | None) -> None:
    from .preflight import GenerationReadinessError

    nodes, models = workflow_asset_gaps(workflow, observed)
    if nodes or models:
        raise GenerationReadinessError(asset_readiness(entry, nodes=nodes, models=models))


# --- Download a default's missing models into the person's own (same-machine) ComfyUI ---------

DOWNLOAD_MARGIN_BYTES = 1 << 30  # free space kept beyond the files: 1 GiB
MODEL_EXTENSIONS = ('.safetensors', '.ckpt', '.pt', '.pth', '.bin', '.gguf')  # what ComfyUI loads as models
_LOOPBACK = frozenset({'127.0.0.1', '::1', 'localhost'})


class AttachedDownloadRefused(Exception):
    """The download step declined; nothing was written, or only verified whole files were."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_folder(paths, key: str | None) -> str | None:
    """The folder whose last segment is the folder key, else the first path.

    ComfyUI lists the legacy `unet` and `clip` folders before `diffusion_models` and `text_encoders`.
    """
    candidates = [path for path in ([paths] if isinstance(paths, str) else paths or ())
                  if isinstance(path, str) and path]
    named = [path for path in candidates if path.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1] == key]
    return (named or candidates or [None])[0]


def _token_safe_source(url: str) -> bool:
    """VibeComfy fetch sends HF_TOKEN to whatever host it fetches: only Hugging Face over https is planned."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return parts.scheme == 'https' and parts.hostname == 'huggingface.co' and parts.username is None


def _size(count: int) -> str:
    return f'{count} bytes' if count < 1 << 20 else f'{count / (1 << 30):.2f} GiB ({count} bytes)'


def _default_sources(entry_id: str) -> dict[str, Any]:
    """Complete declarations only (URL, size, SHA-256): packaged locks and entry model_assets."""
    from .asset_alignment import catalog_manifests
    return {model.destination: model for manifest in catalog_manifests(entry_id)
            for mode in manifest['modes'] for model in mode['models']}


def _served_models(info: Any, declaration: Any) -> frozenset[str]:
    """The model files ComfyUI's own loader lists for this role: the server's list for its folder."""
    node = info.get(declaration.node_class, {}) if isinstance(info, dict) else {}
    inputs = node.get('input', {}) if isinstance(node, dict) else {}
    for group in ('required', 'optional'):
        fields = inputs.get(group, {}) if isinstance(inputs, dict) else {}
        if isinstance(fields, dict) and declaration.input in fields:
            field = fields[declaration.input]
            choices = field[0] if isinstance(field, (tuple, list)) and field else None
            names = choices if isinstance(choices, (tuple, list)) else ()
            return frozenset(name.replace('\\', '/') for name in names
                             if isinstance(name, str) and name.lower().endswith(MODEL_EXTENSIONS))
    return frozenset()


def _local_models(paths) -> frozenset[str]:
    """Model files present in any of the candidate folders the server reports (legacy and canonical alike)."""
    names = set()
    for path in paths:
        root = Path(path)
        if not root.is_dir():
            continue
        for found in root.rglob('*'):
            if found.is_file() and found.name.lower().endswith(MODEL_EXTENSIONS):
                names.add(found.relative_to(root).as_posix())
    return frozenset(names)


def _download_gaps(client, entry_id: str, mode: str):
    """Missing role files of the entry on the attached server, each with its server folder."""
    from astrid.core.model_catalog.registry import ModelRegistry
    from .backends.comfy_target import _COMFY_MODEL_FOLDERS, _resolve_roles

    _entry, mode_spec = ModelRegistry.load_default().get_by_mode(entry_id, mode)
    spec = mode_spec.backends.get('local')
    if spec is None or not spec.model_roles:
        raise AttachedDownloadRefused('no_model_roles', f'{entry_id}/{mode} declares no model files to download.')
    info = client.object_info()
    absent = sorted({role.node_class for role in spec.model_roles.values() if role.node_class not in info})
    if absent:
        raise AttachedDownloadRefused('missing_node', 'This ComfyUI lacks the node(s) ' + ', '.join(absent)
                                      + '; install them first. Nothing was downloaded.')
    _resolved, failures = _resolve_roles(spec, info)
    try:
        listed = client.request_json('/internal/folder_paths', timeout=5, response_limit=64 * 1024)
    except Exception:  # noqa: BLE001 - a failed folder lookup only loses the folder
        listed = None
    folders = listed if isinstance(listed, dict) else {}
    gaps = []
    for role, matches in failures.items():
        if len(matches) > 1:
            continue  # ambiguous: the person keeps exactly one; nothing to download
        declaration = spec.model_roles[role]
        key = _COMFY_MODEL_FOLDERS.get((declaration.node_class, declaration.input))
        candidates = folders.get(key) if key else None
        paths = [p for p in ([candidates] if isinstance(candidates, str) else candidates or ())
                 if isinstance(p, str) and p]
        gaps.append({'role': role, 'key': key, 'names': list(declaration.names),
                     'folder': canonical_folder(folders.get(key) if key else None, key),
                     'served': _served_models(info, declaration), 'local': _local_models(paths)})
    return gaps, spec, info


def plan_attached_model_download(client, entry_id: str, mode: str, *, sources=None) -> dict[str, Any]:
    """Read-only: what would be downloaded where. Writes nothing and asks the server only for reads."""
    from urllib.parse import urlsplit

    gaps, _spec, _info = _download_gaps(client, entry_id, mode)
    sources = _default_sources(entry_id) if sources is None else sources
    from .model_downloader import _validate_model

    files, manual = [], []
    for gap in gaps:
        if gap['key'] and gap['folder'] and gap['served'] != gap['local']:
            # The server's list and this computer's folder disagree: it is not the folder on this computer.
            manual.append({'role': gap['role'], 'file': ' or '.join(gap['names']), 'folder': gap['folder'],
                           'reason': "the folder ComfyUI reports isn't the one on this computer"})
            continue
        model = next((sources[f"{gap['key']}/{name}"] for name in gap['names']
                      if gap['key'] and f"{gap['key']}/{name}" in sources
                      and '/' not in name and '\\' not in name), None)
        reason, url = 'no verified source is declared', None
        if model is not None:
            try:
                url = model.url or _validate_model(model)[1]
            except Exception:  # noqa: BLE001 - an unusable declaration grants nothing
                reason = 'its declared source is not usable'
            else:
                if not _token_safe_source(url):
                    reason, url = 'its source is not https://huggingface.co/, so Astrid will not fetch it', None
        if url is None or not gap['folder']:
            manual.append({'role': gap['role'], 'file': ' or '.join(gap['names']), 'folder': gap['folder'],
                           'reason': reason})
        else:
            files.append({'role': gap['role'], 'file': model.destination.rsplit('/', 1)[-1], 'folder': gap['folder'],
                          'url': url, 'bytes': model.bytes, 'sha256': model.sha256})
    host = urlsplit(client.target.url).hostname
    if host not in _LOOPBACK:
        raise AttachedDownloadRefused('not_same_machine', 'This ComfyUI is not on this computer, so nothing is '
            'downloaded for it. ' + ' '.join(i['message'] for i in attached_model_readiness(
                entry_id, [{'role': g['role'], 'file': ' or '.join(g['names']), 'folder': g['folder']}
                           for g in gaps])['issues']))
    problems = [f['folder'] for f in files if not (os.path.isabs(f['folder']) and os.path.isdir(f['folder'])
                                                    and os.access(f['folder'], os.W_OK))]
    if problems:
        raise AttachedDownloadRefused('folder_not_writable', 'The folder ' + problems[0]
            + ' reported by your ComfyUI does not exist on this computer or is not writable; nothing was downloaded.')
    links = {}
    for f in files:
        if f['folder'] not in links:
            links[f['folder']] = _hard_links_supported(Path(f['folder']))
        f['hard_links'] = links[f['folder']]
    total = sum(f['bytes'] for f in files)
    devices = {}
    for f in files:
        devices.setdefault(os.stat(f['folder']).st_dev, []).append(f)
    free, enough = None, True
    for group in devices.values():
        space = shutil.disk_usage(group[0]['folder']).free
        free = space if free is None else min(free, space)
        enough = enough and space >= sum(f['bytes'] for f in group) + DOWNLOAD_MARGIN_BYTES
    body = {'entry': entry_id, 'mode': mode, 'target': client.target.url, 'files': files, 'manual': manual}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {**body, 'total_bytes': total, 'margin_bytes': DOWNLOAD_MARGIN_BYTES, 'free_bytes': free,
            'space_ok': enough, 'plan_sha256': digest}


def render_attached_download_plan(plan: dict[str, Any]) -> str:
    if not plan['files'] and not plan['manual']:
        return f"Nothing is missing for {plan['entry']} {plan['mode']} on your ComfyUI at {plan['target']}; it is ready."
    lines = [f"Missing models for {plan['entry']}/{plan['mode']} on your ComfyUI at {plan['target']}:"]
    for f in plan['files']:
        lines.append(f"  {f['file']}  {_size(f['bytes'])}  -> {f['folder']}  (from {f['url']}, SHA-256 {f['sha256']})")
    for m in plan['manual']:
        lines.append(f"  {m['file']}: {m.get('reason', 'no verified source is declared')}; put {m['file']} in "
                     f"{m['folder'] or 'the models folder for ' + str(m['role'])} yourself.")
    if not plan['files']:
        return '\n'.join(lines + ['Nothing can be downloaded.'])
    lines.append(f"Total: {_size(plan['total_bytes'])}; free space: {_size(plan['free_bytes'])}, "
                 f"and {_size(plan['margin_bytes'])} more is kept free as a margin.")
    if not plan['space_ok']:
        lines.append('Not enough free space for this download; free some space first.')
    lines.append('Nothing has been written. ComfyUI is not restarted and nothing else on it is changed.')
    lines.append(f"To download exactly this, run: astrid generate fetch-models {plan['entry']} {plan['mode']} "
                 f"--comfy-url {plan['target']} --apply {plan['plan_sha256']}")
    return '\n'.join(lines)


def _hard_links_supported(folder: Path) -> bool:
    """Probe with a tiny temporary file: FAT, exFAT and some network mounts cannot hard-link."""
    fd, name = tempfile.mkstemp(dir=folder, prefix='.astrid-link-probe-')
    os.close(fd)
    probe = Path(name)
    twin = probe.with_name(probe.name + '.twin')
    try:
        os.link(probe, twin)
        return True
    except OSError:
        return False
    finally:
        for path in (twin, probe):
            try:
                path.unlink()
            except OSError:
                pass


def _publish(staged: Path, final: Path, *, hard_links: bool) -> None:
    """Give the verified staged file its final name without ever overwriting another file.

    Without hard links, an exclusive create claims the final name first; the staged file then replaces that placeholder.
    """
    if hard_links:
        try:
            os.link(staged, final)  # fails if the file appeared meanwhile: never overwrites
            return
        except FileExistsError as exc:
            raise AttachedDownloadRefused('file_exists', f'{final} appeared while downloading; left untouched.') from exc
        except OSError:
            pass  # no hard links on this folder: claim the name exclusively instead
    try:
        os.close(os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError as exc:
        raise AttachedDownloadRefused('file_exists', f'{final} appeared while downloading; left untouched.') from exc
    try:
        os.replace(staged, final)
    except BaseException:
        final.unlink(missing_ok=True)  # our own placeholder only: the name was claimed exclusively above
        raise


def _fetch_into_folder(item: dict[str, Any], http_client) -> None:
    """One verified file via VibeComfy fetch, staged under a temp name in the same folder, then renamed."""
    from vibecomfy import fetch

    folder = Path(item['folder']).resolve(strict=True)
    root, final = folder.parent, folder / item['file']
    # A unique staging name claimed exclusively (O_EXCL), so a file the person already has is never touched. The
    # empty claim is kept: fetch.download only reuses a non-empty destination and otherwise replaces it with its own
    # temp file, so the name stays this call's own until it is removed below.
    fd, claimed = tempfile.mkstemp(dir=folder, prefix='.' + item['file'] + '.', suffix='.astrid-partial')
    os.close(fd)
    staged = Path(claimed)
    entry = {'name': staged.name, 'subdir': folder.name, 'url': item['url'],
             'sha256': item['sha256'], 'size_bytes': item['bytes']}
    try:
        receipt = fetch._verification_receipt_path(staged, root=root)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    kept = [d for d in (receipt.parent, receipt.parent.parent) if d.exists()]
    try:
        fetch.download(entry, root=root, client=http_client, quiet=True)
        _publish(staged, final, hard_links=item.get('hard_links', True))
    finally:
        staged.unlink(missing_ok=True)
        receipt.unlink(missing_ok=True)
        for directory in (receipt.parent, receipt.parent.parent):
            if directory not in kept:
                try:
                    directory.rmdir()
                except OSError:
                    pass


def apply_attached_model_download(client, entry_id: str, mode: str, *, plan_sha256, sources=None,
                                  http_client=None) -> dict[str, Any]:
    """Download exactly the plan whose digest was confirmed; re-checks everything before the first write."""
    if not isinstance(plan_sha256, str) or not re.fullmatch('[0-9a-f]{64}', plan_sha256):
        raise AttachedDownloadRefused('confirmation_required', 'Run without --apply to see the plan, then pass its '
                                      'plan digest to --apply. Nothing was written.')
    plan = plan_attached_model_download(client, entry_id, mode, sources=sources)
    if plan['plan_sha256'] != plan_sha256:
        raise AttachedDownloadRefused('plan_changed', 'The plan changed since it was shown (files, sources or folders); '
                                      'review the new plan and confirm it. Nothing was written.')
    if not plan['files']:
        raise AttachedDownloadRefused('nothing_to_download', 'Nothing in the plan can be downloaded.')
    if not plan['space_ok']:
        raise AttachedDownloadRefused('insufficient_space',
            f"Not enough free space: {_size(plan['free_bytes'])} free, need {_size(plan['total_bytes'])} plus a "
            f"margin of {_size(plan['margin_bytes'])}. Nothing was written.")
    for f in plan['files']:
        if os.path.lexists(os.path.join(f['folder'], f['file'])):
            raise AttachedDownloadRefused('file_exists', f"{f['file']} already exists in {f['folder']}; it is never "
                                          'overwritten. Nothing was written.')
    done = []
    for f in plan['files']:
        _fetch_into_folder(f, http_client)
        done.append({'file': f['file'], 'folder': f['folder'], 'bytes': f['bytes']})
    return {'downloaded': done, 'total_bytes': plan['total_bytes'], 'restarted': False,
            'next_step': 'No restart needed: ComfyUI re-reads a model folder when its modification time changes, so '
                         'the new files appear on its next /object_info request (refresh the node definitions in an '
                         'open ComfyUI page, or reload it).'}


def main(args) -> int:
    from .backends.comfy_target import ComfyTarget, ComfyTargetClient

    try:
        client = ComfyTargetClient(ComfyTarget(args.comfy_url, 'attached'))
        if args.apply:
            result = apply_attached_model_download(client, args.entry, args.mode, plan_sha256=args.apply)
            text = '\n'.join([*(f"Downloaded {d['file']} -> {d['folder']}" for d in result['downloaded']),
                              result['next_step']])
        else:
            result = plan_attached_model_download(client, args.entry, args.mode)
            text = render_attached_download_plan(result)
    except (AttachedDownloadRefused, ValueError, OSError, KeyError, RuntimeError) as exc:
        print(json.dumps({'error': {'code': getattr(exc, 'code', 'download_failed'), 'message': str(exc)}},
                         sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True) if args.json else text)
    return 0
