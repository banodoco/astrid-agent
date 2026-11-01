"""Shared read-only workflow asset diagnostics, before canonical approval."""
from __future__ import annotations

import shlex
from typing import Any


def align_argv(entry: str | None = None) -> list[str]:
    return ['astrid', 'generate', 'align', '--apply', '--consent', 'user_env',
            *(['--entry', entry] if entry else ['--all'])]


def asset_readiness(entry: str | None, *, nodes=(), models=(), target=None) -> dict[str, Any]:
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
