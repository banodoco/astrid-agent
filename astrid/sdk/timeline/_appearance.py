"""Package-owned placement through the pinned local Node runtime."""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess

from astrid.core.rendering._remotion_package import composition_package_problem
from astrid.core.rendering.node_pin import resolve_pinned_node
from astrid.core.rendering.remotion_runtime import resolve_remotion_project_dir
from ._command_state import CommandError, equal


_FIELDS = ('x', 'y', 'width', 'height', 'cropTop', 'cropBottom', 'cropLeft', 'cropRight', 'fit')


def package_query(request):
    project = resolve_remotion_project_dir()
    problem = composition_package_problem(project)
    if problem:
        raise CommandError(problem)
    node = resolve_pinned_node(project, override=os.environ.get('ASTRID_NODE_EXECUTABLE') or None)
    if not node.ok:
        raise CommandError(node.problem or 'pinned Node unavailable')
    try:
        result = subprocess.run([str(node.path), str(project / 'placement-query.mjs')],
                                input=json.dumps(request, allow_nan=False), text=True,
                                capture_output=True, cwd=project, timeout=30, check=False)
        if result.returncode:
            raise CommandError(result.stderr.strip() or 'placement package failed')
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise CommandError(f'placement package failed: {exc}') from exc


def canvas(config, ref):
    output = config.get('output') or {}
    match = re.fullmatch(r'([1-9][0-9]*)[xX]([1-9][0-9]*)', str(output.get('resolution', '')))
    if not match:
        raise CommandError(f'{ref} property output.resolution is missing or invalid')
    return {'width': int(match[1]), 'height': int(match[2])}


def normalize_owner(ctx, ref):
    """Capture all normalizer dependencies and record its bounded changes."""
    owner = ref.rsplit('/', 1)[0]
    findings = []
    if owner != 'parent':
        findings.extend(_normalize_one(ctx, 'parent', ref))
    findings.extend(_normalize_one(ctx, owner, ref))
    return findings


def _normalize_one(ctx, owner, ref):
    config = copy.deepcopy(ctx.state.config(owner))
    source = 'parent' if owner == 'parent' else owner + '/source'
    prefix = ['config'] if owner == 'parent' else []
    ctx.read(source, prefix + ['output'])
    ctx.read(source, prefix + ['theme_overrides', 'visual', 'canvas'])
    clips = ctx.select('collection.clips', owner)
    tracks = ctx.select('collection.tracks', owner)
    config['clips'] = []
    config['tracks'] = []
    for entity in tracks + clips:
        row = ctx.read(entity, [])
        (config['tracks'] if '/track:' in entity else config['clips']).append(row)
    result = package_query({'operation': 'normalize', 'config': config})
    normalized = result['config']
    canvas(normalized, ref)
    old_output, new_output = config.get('output'), normalized.get('output')
    if old_output != new_output:
        if 'output' in config and old_output is None:
            raise CommandError(f'{ref} property output is null and cannot be materialized safely')
        old_fields = old_output or {}
        if (not isinstance(old_fields, dict) or not isinstance(new_output, dict)
                or any(new_output.get(key) != value for key, value in old_fields.items())
                or set(new_output) - set(old_fields) - {'resolution', 'fps', 'file'}):
            raise CommandError('placement normalizer changed unsupported output property')
        if owner == 'parent' and old_output is None and any(
                item.get('kind') == 'canvas' and item.get('source') == 'theme_overrides.visual.canvas'
                for item in result.get('findings', [])):
            ctx.dependency('placement.canvas.materialization', 'entity', {
                'ref': source, 'path': prefix + ['output'], 'after': copy.deepcopy(new_output),
                'authored_canvas': copy.deepcopy(config.get('theme_overrides', {}).get('visual', {}).get('canvas'))})
        ctx.write(source, prefix + ['output'], new_output)
    for kind, refs in (('track', tracks), ('clip', clips)):
        before = config[kind + 's']
        after = normalized[kind + 's']
        if len(before) != len(after):
            raise CommandError('placement normalizer changed collection membership')
        allowed = ('app',) if kind == 'track' else _FIELDS
        for entity, old, new in zip(refs, before, after):
            if old.get('id') != new.get('id'):
                raise CommandError('placement normalizer changed identity')
            for key in set(old) | set(new):
                if equal(old.get(key), new.get(key)) and (key in old) == (key in new):
                    continue
                if key not in allowed or key not in new:
                    raise CommandError(f'placement normalizer changed unsupported {kind} property {key}')
                if key == 'app':
                    old_app, new_app = old.get('app') or {}, new[key]
                    if not isinstance(old_app, dict) or not isinstance(new_app, dict) or any(
                        old_app.get(k) != new_app.get(k) for k in set(old_app) | set(new_app)
                        if k not in ('clipPlacementVersion', 'scaleAppliesToPositionedClips')):
                        raise CommandError('placement normalizer changed unsupported track app metadata')
                ctx.write(entity, [key], new[key])
    return result.get('findings', [])


def normalize_revision_closure(parent, internals):
    """Normalize copies while each source still has its own tracks and canvas."""
    parent, internals = copy.deepcopy(parent), copy.deepcopy(internals)
    payload = parent['payload']
    config = payload['config']
    separate = payload.get('clips', [])
    embedded = config.get('clips', [])
    if separate and embedded and separate != embedded:
        raise CommandError('parent clip collections disagree before placement normalization')
    config['clips'] = copy.deepcopy(separate or embedded)
    media = lambda row: any(isinstance(c, dict) and c.get('clipType', 'media') in ('media', 'image', 'video') for c in row.get('clips', []))
    needs_canvas = media(config) or any(media(row['payload']) for row in internals)
    if not needs_canvas:
        return parent, internals, []
    result = package_query({'operation': 'normalize', 'config': config})
    canvas(result['config'], 'parent timeline')
    payload['config'] = result['config']
    if separate:
        payload['clips'] = copy.deepcopy(result['config']['clips'])
    findings = [{'owner': 'parent', **item} for item in result['findings']]
    for row in internals:
        child = row['payload']
        if not media(child):
            continue
        normalized = package_query({'operation': 'normalize', 'config': child})
        canvas(normalized['config'], f"internal timeline {row.get('revision_id')}")
        row['payload'] = normalized['config']
        findings.extend({'owner': row.get('revision_id'), **item} for item in normalized['findings'])
    return parent, internals, findings
