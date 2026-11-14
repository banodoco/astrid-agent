"""Shared read-only workflow asset diagnostics, before canonical approval."""
from __future__ import annotations

import shlex
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
