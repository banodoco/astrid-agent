"""Shared read-only workflow asset diagnostics, before canonical approval."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def declared_model_specs(assets: Any, *, require_complete: bool = True,
                         incomplete: list[dict[str, Any]] | None = None,
                         entry_id: str | None = None):
    """Project canonical workflow declarations into the existing downloader type.

    Incomplete declarations grant no custody authority. Inventory composition
    may ignore them; provisioning a selected entry must report them instead.
    An explicit incomplete collector lets inspection report missing fields
    without granting those declarations custody authority. Invalid complete
    declarations still fail validation.
    """
    from .engine_contracts import EngineError, IntegrityError, ModelLockValidationError, ModelSpec
    from .model_downloader import _safe_relative, _validate_model
    from .model_declarations import IncompleteModelDeclaration

    if not isinstance(assets, (list, tuple)):
        raise ModelLockValidationError('entry model_assets must be a list')
    models = {}
    def record_gap(record):
        if incomplete is not None:
            incomplete.append(record)
        if not require_complete:
            # An inventory-only read may exclude a declaration that grants
            # no custody authority, but must record the excluded fact.
            import logging

            logging.getLogger(__name__).debug(
                "Excluded non-provisionable model declaration for entry %s: %s",
                entry_id or "(unselected)", record.get("missing") or record.get("code"),
            )

    for asset in assets:
        if not isinstance(asset, dict):
            raise ModelLockValidationError('entry model_assets must contain objects')
        missing = [key for key in ('name', 'url', 'sha256', 'size_bytes')
                   if asset.get(key) is None or asset.get(key) == '']
        if missing:
            record = {'name': asset.get('name'), 'subdir': asset.get('subdir'), 'missing': missing}
            if require_complete and incomplete is None:
                raise IncompleteModelDeclaration(record, entry_id=entry_id)
            record_gap(record)
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
        except EngineError as exc:
            if require_complete:
                raise
            # A non-provisionable, unselected declaration grants no authority.
            # The exact-root verifier will still reject any unlisted file.
            record_gap({'name': asset.get('name'), 'subdir': asset.get('subdir'),
                        'missing': [], 'code': exc.code})
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
    from .preflight import readiness_envelope
    from .setup_repairs import manual_setup_fix

    selected = {'id': entry, 'engine': 'comfyui'}
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
        fix_id = f"model-{len(fixes) + 1}"
        issues.append({'code': 'missing_model', 'severity': 'error',
                       'message': message,
                       'details': {'model': file, 'role': role, 'folder': folder},
                       'fix_ids': [fix_id]})
        fixes.append(manual_setup_fix(fix_id, action, entry=selected, target=target,
                                     scope='attached_target', details={'action': action}))
    return readiness_envelope(entry=selected, target=target, issues=issues, fixes=fixes)


def _is_comfyui_core_repo(repo: str) -> bool:
    """A pack whose repository is ComfyUI itself ships with ComfyUI, not as a custom node."""
    from urllib.parse import urlsplit

    parsed = urlsplit(repo)
    if parsed.hostname != 'github.com':
        return False
    return parsed.path.lower().rstrip('/').removesuffix('.git') in {
        '/comfyanonymous/comfyui', '/comfy-org/comfyui',
    }


def _locked_commit(lock: Any, pack: Any) -> str | None:
    """The commit the lock pins for this pack's repository, if the lock declares one."""
    from vibecomfy.node_packs import read_lockfile

    for entry in read_lockfile(lock):
        if entry.url == pack.repo and (entry.git_commit_sha or entry.commit):
            return entry.git_commit_sha or entry.commit
    return None


def _pack_install_commands(url: str, commit: str | None, custom_nodes: str | None):
    """Argv lists to clone a custom node pack (and check out its pinned commit), plus a paste-safe renderer.

    With no real custom_nodes folder, a placeholder stands in; its style follows sys.platform.
    """
    if custom_nodes is None:
        windows = sys.platform == 'win32'
        base = '<your ComfyUI folder>' + ('\\custom_nodes' if windows else '/custom_nodes')
    else:
        windows = '\\' in custom_nodes or bool(re.match(r'[A-Za-z]:', custom_nodes))
        base = custom_nodes
    sep = '\\' if windows else '/'
    folder = base.rstrip('\\/') + sep + url.rstrip('/').rsplit('/', 1)[-1].removesuffix('.git')
    commands = [['git', 'clone', url, folder]]
    if commit:
        commands.append(['git', '-C', folder, 'checkout', commit])
    render = subprocess.list2cmdline if windows else shlex.join
    return commands, render


PACK_AFTER = ('If the pack has a requirements.txt, install it with the Python your ComfyUI runs on, '
              'then restart ComfyUI and retry.')


def attached_asset_readiness(entry: str | None, *, nodes=(), models=(), lockfile_path=None, target=None,
                             custom_nodes: str | None = None) -> dict[str, Any]:
    """Readiness for an attached ComfyUI: the person fixes it on their ComfyUI; Astrid installs and restarts nothing there.

    Core nodes use the shipped core-class evidence, never absence of a pack.
    Known custom packs retain their manual installation guidance; unknown
    classes name the missing provider and require identification, not an
    unrelated ComfyUI update. Astrid installs and restarts nothing there.
    """
    from vibecomfy.node_packs import resolve_lockfile_path, resolve_node_packs
    from .node_class_authority import core_node_classes
    from .preflight import readiness_envelope
    from .setup_repairs import manual_setup_fix

    selected = {'id': entry, 'engine': 'comfyui'}
    lock = resolve_lockfile_path(lockfile_path)
    issues, fixes = [], []
    fixed_packs: dict[str, str] = {}
    nodes = tuple(nodes)
    core = core_node_classes(nodes)
    for name in nodes:
        providers = resolve_node_packs({name}, lockfile_path=lock)
        packs = [pack for pack in providers if not _is_comfyui_core_repo(pack.repo)]
        commands = None
        if packs:
            pack = packs[0]
            url = pack.repo if pack.repo.startswith(('http://', 'https://', 'git@')) else None
            where = f' ({url})' if url else ''
            action = (f"Install {pack.name}{where} in your ComfyUI's custom_nodes folder, "
                      "then restart ComfyUI yourself and retry.")
            details = {'node': name, 'pack': pack.name, 'repository': url}
            if url:
                argv, render = _pack_install_commands(url, _locked_commit(lock, pack), custom_nodes)
                action += f" Commands: {' ; '.join(render(cmd) for cmd in argv)}. {PACK_AFTER}"
                details.update(commands=argv, after=PACK_AFTER)
                commands = argv
            message = f'Your ComfyUI does not have {name}, which comes from the custom node pack {pack.name}. {action}'
        elif name in core or any(_is_comfyui_core_repo(pack.repo) for pack in providers):
            action = 'Update ComfyUI, then retry.'
            message = (f'Your ComfyUI does not have {name}, which is part of ComfyUI itself; '
                       'update ComfyUI, then retry.')
            details = {'node': name, 'pack': None, 'repository': None}
        else:
            action = ('Identify the providing custom node pack and its exact source before installing '
                      'it on your ComfyUI; restart ComfyUI yourself, then retry.')
            message = f'Your ComfyUI does not have {name}; no known providing pack. {action}'
            details = {'node': name, 'pack': None, 'repository': None}
        # Every class links to its own repair, including subsequent classes
        # sharing the first pack's one manual installation instruction.
        fix_id = fixed_packs.get(pack.name) if commands is not None else None
        if fix_id is None:
            fix_id = f"node-{len(fixes) + 1}"
            fix_details = {'action': action}
            if commands is not None:
                fixed_packs[pack.name] = fix_id
                fix_details['commands'] = commands
            fixes.append(manual_setup_fix(fix_id, action, entry=selected, target=target,
                                         scope='attached_target', details=fix_details))
        issues.append({'code': 'missing_node_pack', 'severity': 'error', 'message': message,
                       'details': details, 'fix_ids': [fix_id]})
    for name in models:
        action = 'Put the model file in its models folder on your ComfyUI yourself, then retry.'
        fix_id = f"model-{len(fixes) + 1}"
        issues.append({'code': 'missing_model', 'severity': 'error',
                       'message': f'Missing model {name} on your ComfyUI. {action}',
                       'details': {'model': name}, 'fix_ids': [fix_id]})
        fixes.append(manual_setup_fix(fix_id, action, entry=selected, target=target,
                                     scope='attached_target', details={'action': action}))
    return readiness_envelope(entry=selected, target=target, issues=issues, fixes=fixes)


def asset_readiness(entry: str | None, *, nodes=(), models=(), target=None, placements=None) -> dict[str, Any]:
    from .preflight import readiness_envelope

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
    fixes = []
    if issues:
        for issue in issues:
            issue['fix_ids'] = ['align-locked-assets']
        # Preserve the concrete recovery command for managed gaps. The
        # caller still needs explicit user_env consent; the command is not
        # a grant of approval for unreviewed node code.
        fixes = [{'id': 'align-locked-assets', 'who': 'agent', 'argv': argv, 'scope': 'user_env',
                  'details': {'action': 'Install missing locked assets on the managed target; unlocked assets require a manifest.'}}]
    return readiness_envelope(entry={'id': entry, 'engine': 'comfyui'}, target=target,
                              issues=issues, fixes=fixes)


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
_PROGRESS_STEP = 256 << 20  # bytes between progress lines
_PROGRESS_SECONDS = 15
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
    """Missing role files of the entry on the attached server, each with its server folder.

    Also returns the roles the server already lists (`present`): they are part of the plan's digest.
    """
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
    present = []
    for role, name in _resolved.items():
        declaration = spec.model_roles[role]
        key = _COMFY_MODEL_FOLDERS.get((declaration.node_class, declaration.input))
        present.append({'role': role, 'key': key, 'name': name,
                        'folder': canonical_folder(folders.get(key) if key else None, key)})
    return gaps, present, spec, info


def _source_url(model) -> tuple[str | None, str]:
    """The URL to fetch a declared model from, or None with the reason no verified source is usable."""
    from .model_downloader import _validate_model

    if model is None:
        return None, 'no verified source is declared'
    try:
        url = model.url or _validate_model(model)[1]
    except Exception:  # noqa: BLE001 - an unusable declaration grants nothing
        return None, 'its declared source is not usable'
    if not _token_safe_source(url):
        return None, 'its source is not https://huggingface.co/, so Astrid will not fetch it'
    return url, ''


def plan_attached_model_download(client, entry_id: str, mode: str, *, sources=None) -> dict[str, Any]:
    """Read-only: what would be downloaded where. Writes nothing and asks the server only for reads.

    `files` are still missing; `present` are already listed by the server with a verified source. The
    digest covers both (name, URL, SHA-256, size, folder), so a partly finished run keeps the same digest.
    """
    from urllib.parse import urlsplit

    gaps, present, _spec, _info = _download_gaps(client, entry_id, mode)
    sources = _default_sources(entry_id) if sources is None else sources

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
        url, reason = _source_url(model)
        if url is None or not gap['folder']:
            manual.append({'role': gap['role'], 'file': ' or '.join(gap['names']), 'folder': gap['folder'],
                           'reason': reason or 'no verified source is declared'})
        else:
            files.append({'role': gap['role'], 'file': model.destination.rsplit('/', 1)[-1], 'folder': gap['folder'],
                          'url': url, 'bytes': model.bytes, 'sha256': model.sha256})
    present_files = []
    for row in present:
        if not row['key'] or not row['folder'] or '/' in row['name'] or '\\' in row['name']:
            continue
        model = sources.get(f"{row['key']}/{row['name']}")
        url, _reason = _source_url(model)
        if url is not None:
            present_files.append({'role': row['role'], 'file': row['name'], 'folder': row['folder'],
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
    wanted = sorted(({key: f[key] for key in ('file', 'folder', 'url', 'bytes', 'sha256')}
                     for f in [*files, *present_files]), key=lambda f: (f['folder'], f['file']))
    body = {'entry': entry_id, 'mode': mode, 'target': client.target.url, 'wanted': wanted, 'manual': manual}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'entry': entry_id, 'mode': mode, 'target': client.target.url, 'files': files, 'present': present_files,
            'manual': manual, 'total_bytes': total, 'margin_bytes': DOWNLOAD_MARGIN_BYTES, 'free_bytes': free,
            'space_ok': enough, 'plan_sha256': digest}


def render_attached_download_plan(plan: dict[str, Any]) -> str:
    if not plan['files'] and not plan['manual']:
        return f"Nothing is missing for {plan['entry']} {plan['mode']} on your ComfyUI at {plan['target']}; it is ready."
    lines = [f"Missing models for {plan['entry']}/{plan['mode']} on your ComfyUI at {plan['target']}:"]
    for f in plan['files']:
        lines.append(f"  {f['file']}  {_size(f['bytes'])}  -> {f['folder']}  (from {f['url']}, SHA-256 {f['sha256']})")
    for p in plan['present']:
        lines.append(f"  {p['file']}  already on your ComfyUI in {p['folder']}; not downloaded again")
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


def _say(line: str) -> None:
    """Progress goes to stderr, so `--json` keeps stdout to the result alone."""
    print(line, file=sys.stderr, flush=True)


def _progress(name: str, total: int):
    """A callback for fetch_verified: a line every 256 MiB, or every 15 seconds while bytes keep arriving."""
    state = {'shown': 0, 'at': time.monotonic()}

    def report(done: int) -> None:
        now = time.monotonic()
        if done - state['shown'] >= _PROGRESS_STEP or (done > state['shown'] and now - state['at'] >= _PROGRESS_SECONDS):
            state['shown'], state['at'] = done, now
            _say(f'  {name}: {_size(done)} of {_size(total)}')
    return report


def _fetch_into_folder(item: dict[str, Any], http_client, *, progress=None) -> int:
    """One verified file via VibeComfy fetch, staged under a temp name in the same folder, then renamed.

    An interrupted transfer keeps its bytes in a `.<file>.astrid-resume` part, so the next run resumes it.
    Returns the byte offset this transfer resumed from.
    """
    from vibecomfy import fetch

    from .asset_alignment import fetch_verified

    folder = Path(item['folder']).resolve(strict=True)
    root, final = folder.parent, folder / item['file']
    part = folder / f".{item['file']}.astrid-resume"
    if part.is_symlink() or (part.exists() and (not part.is_file() or part.stat().st_nlink != 1)):
        raise AttachedDownloadRefused('unsafe_partial', f'{part} is not a plain partial download; it is left untouched.')
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
        resumed = fetch_verified(entry, staged, root=root, part=part, label=item['file'], client=http_client,
                                 progress=progress)
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
    return resumed


def _stopped(plan: dict[str, Any], done: list[dict[str, Any]], failed: dict[str, Any], exc: Exception) -> AttachedDownloadRefused:
    """Say truthfully what this run finished and what it did not, so the same digest can be confirmed again."""
    from astrid.core.contracts.errors import TLS_VERIFICATION_CODE, coerce_astrid_error

    code = exc.code if isinstance(exc, AttachedDownloadRefused) else 'download_failed'
    structured = coerce_astrid_error(exc)
    if structured.code == TLS_VERIFICATION_CODE:
        code = TLS_VERIFICATION_CODE
        exc = structured
    finished = ', '.join(d['file'] for d in done) or 'none (no file was published)'
    rest = ', '.join(f['file'] for f in plan['files'][plan['files'].index(failed) + 1:]) or 'none'
    part = Path(failed['folder']) / f".{failed['file']}.astrid-resume"
    kept = ' Its partial download is kept, so the next run resumes it.' if part.exists() else ''
    return AttachedDownloadRefused(code, f"Stopped at {failed['file']}: {exc}.{kept} Finished in this run: {finished}. "
        f"Not downloaded: {rest}. Run the same command again to fetch only what is missing.")


def _verify_confirmed_present(item: dict[str, Any]) -> dict[str, Any]:
    """Verify one file ComfyUI lists against its digest-bound local declaration."""
    path = Path(item['folder']) / item['file']
    if not path.is_file():
        if path.is_symlink() and not path.exists():
            raise AttachedDownloadRefused('present_file_missing',
                f"{item['file']} is listed by ComfyUI but is missing from {path}; no files were downloaded.")
        if not os.path.lexists(path):
            raise AttachedDownloadRefused('present_file_missing',
                f"{item['file']} is listed by ComfyUI but is missing from {path}; no files were downloaded.")
        raise AttachedDownloadRefused('present_file_unreadable',
            f"{item['file']} is listed by ComfyUI but is not a regular file at {path}; no files were downloaded.")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open('rb') as stream:
            while block := stream.read(8 << 20):
                size += len(block)
                digest.update(block)
    except FileNotFoundError as exc:
        raise AttachedDownloadRefused('present_file_missing',
            f"{item['file']} is listed by ComfyUI but is missing from {path}; no files were downloaded.") from exc
    except OSError as exc:
        raise AttachedDownloadRefused('present_file_unreadable',
            f"{item['file']} is listed by ComfyUI but could not be verified at {path}: {exc}; "
            "no files were downloaded.") from exc
    actual_sha = digest.hexdigest()
    if size != item['bytes'] or actual_sha != item['sha256']:
        raise AttachedDownloadRefused('present_file_mismatch',
            f"{item['file']} is listed by ComfyUI but does not match the confirmed file at {path} "
            f"(expected {item['bytes']} bytes and SHA-256 {item['sha256']}; found {size} bytes and "
            f"SHA-256 {actual_sha}); no files were downloaded.")
    return {'file': item['file'], 'folder': item['folder'], 'bytes': size, 'sha256': actual_sha}


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
    verified_present = [_verify_confirmed_present(item) for item in plan['present']]
    if not plan['files']:
        if verified_present and not plan['manual']:
            return {'downloaded': [], 'present': [item['file'] for item in verified_present],
                    'done': verified_present, 'total_bytes': 0, 'restarted': False,
                    'next_step': 'All requested model files are present and verified. Nothing needed downloading.'}
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
        _say(f"Downloading {f['file']} ({_size(f['bytes'])}) -> {f['folder']}")
        try:
            resumed = _fetch_into_folder(f, http_client, progress=_progress(f['file'], f['bytes']))
        except Exception as exc:  # noqa: BLE001 - every failure is reported with what finished before it
            raise _stopped(plan, done, f, exc) from exc
        done.append({'file': f['file'], 'folder': f['folder'], 'bytes': f['bytes'], 'resumed_from_bytes': resumed})
        suffix = f', resumed from {_size(resumed)}' if resumed else ''
        _say(f"Downloaded {f['file']} ({_size(f['bytes'])}{suffix})")
    return {'downloaded': done, 'present': [p['file'] for p in verified_present],
            'done': [*verified_present, *done], 'total_bytes': plan['total_bytes'],
            'restarted': False,
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
        from .preflight import GenerationReadinessError, generation_error
        from .setup_repairs import manual_setup_fix
        from .setup_contract import repair_step

        if isinstance(exc, GenerationReadinessError):
            raise
        selected = {'id': args.entry, 'mode': args.mode, 'engine': 'comfyui'}
        target = {'endpoint': args.comfy_url, 'owner': 'human'}
        error = generation_error(exc, entry=selected, target=target)
        # Plan confirmation remains an explicit later human decision. The
        # repair re-inspects the exact attached target, never auto-applies.
        code = error.code
        step = repair_step(code)
        fix = manual_setup_fix('inspect-attached-model-plan', step, entry=selected,
                               target=target, scope='attached_target', code=code)
        if code not in {'placement_invalid', 'target_unreachable'}:
            fix['instruction']['then_argv'] = [
                'astrid', 'generate', 'fetch-models', args.entry, args.mode,
                '--comfy-url', args.comfy_url, '--json',
            ]
            fix['placeholders'] = {}
        issue = {**error.readiness['issues'][0], 'fix_ids': [fix['id']]}
        from .preflight import readiness_envelope

        raise GenerationReadinessError(readiness_envelope(
            entry=selected, target=target, issues=[issue], fixes=[fix])) from exc
    print(json.dumps(result, sort_keys=True) if args.json else text)
    return 0
