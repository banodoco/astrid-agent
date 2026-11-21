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
from astrid.core.timeline.track_appearance import (
    CLIP_PLACEMENT_VERSION,
    is_legacy_clip_placement_version,
)

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
        raise CommandError(f'{ref} property output.resolution is missing or invalid; authored timeline output is unavailable; a guarded CLI output command is not yet available')
    return {'width': int(match[1]), 'height': int(match[2])}


def canonicalize_rect_aliases(config):
    """Translate the supported rect spelling on a copy before package normalization."""
    result = copy.deepcopy(config)
    findings = []
    for clip in result.get('clips', []):
        if not isinstance(clip, dict) or 'rect' not in clip:
            continue
        rect = clip.pop('rect')
        if rect is not None:
            if not isinstance(rect, dict) or set(rect) - {'x', 'y', 'width', 'height'}:
                raise CommandError(f"clip {clip.get('id', '?')!r} property rect is invalid")
            for key, value in rect.items():
                if key in clip and clip[key] != value:
                    raise CommandError(f"clip {clip.get('id', '?')!r} property rect.{key} conflicts with {key}")
                clip[key] = copy.deepcopy(value)
        findings.append({'kind': 'rect-alias', 'clipId': clip.get('id'), 'before': rect})
    return result, findings


def in_memory_canvas_warnings(findings):
    """Report absent-output materializations with their exact package provenance."""
    warnings = []
    for finding in findings:
        if (not isinstance(finding, dict) or finding.get('kind') != 'canvas'
                or not isinstance(finding.get('source'), str)
                or finding.get('before') is not None):
            continue
        output = finding.get('after') or {}
        source = finding['source']
        if source == 'legacy-render-resolved':
            message = (f"Legacy render-resolved canvas {output.get('resolution')} at {output.get('fps')} fps "
                       "was used in memory; the timeline was not changed. It was not written to the timeline.")
        else:
            message = (f"Canvas {output.get('resolution')} at {output.get('fps')} fps from {source} "
                       "was used in memory and not written to the timeline.")
        warnings.append({**finding, 'message': message})
    return warnings


def normalize_for_read(config, *, parent_canvas=False):
    """Normalize a copy, supplying only the existing export resolver's legacy fact.

    The optional fact is never used by guarded authored edits and is not stored.
    """
    canonical, aliases = canonicalize_rect_aliases(config)
    if parent_canvas and 'output' in canonical:
        output = canonical['output']
        if not isinstance(output, dict):
            raise CommandError('timeline property output is invalid; expected a complete authored output')
        if not {'resolution', 'fps', 'file'} <= set(output):
            raise CommandError('timeline property output is incomplete; resolution, fps and file are required')
    request = {'operation': 'normalize', 'config': canonical}
    if parent_canvas and 'output' not in canonical:
        from astrid.core.rendering.profile import resolve_render_profile
        profile = resolve_render_profile(canonical)
        request['options'] = {'legacyCanvas': {'width': profile.width, 'height': profile.height,
            'fps': profile.fps_rational[0] / profile.fps_rational[1], 'source': 'legacy-render-resolved'}}
    normalized = package_query(request)
    if parent_canvas and 'output' in normalized['config']:
        output = normalized['config']['output']
        if not isinstance(output, dict) or not {'resolution', 'fps', 'file'} <= set(output):
            raise CommandError('timeline property output is incomplete; resolution, fps and file are required')
    normalized['findings'] = aliases + normalized['findings']
    return normalized


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
    canonical, alias_findings = canonicalize_rect_aliases(config)
    result = package_query({'operation': 'normalize', 'config': canonical})
    normalized = result['config']
    if owner == 'parent' or not any(finding.get('kind') == 'canvas-not-required' for finding in result['findings']):
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
                if kind == 'clip' and key == 'rect' and key not in new:
                    ctx.clear(entity, [key])
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
    return alias_findings + result.get('findings', [])


def _omit_legacy_placement_version(config):
    tracks = config.get('tracks')
    if isinstance(tracks, list):
        for track in tracks:
            app = track.get('app') if isinstance(track, dict) else None
            if isinstance(app, dict) and is_legacy_clip_placement_version(app.get(CLIP_PLACEMENT_VERSION)):
                del app[CLIP_PLACEMENT_VERSION]


def omit_legacy_placement_markers(parent, internals):
    """Treat the R13 marker as absent on detached reader copies."""
    config = parent['payload'].get('config')
    if isinstance(config, dict):
        _omit_legacy_placement_version(config)
    for row in internals:
        _omit_legacy_placement_version(row['payload'])


def normalize_revision_closure(parent, internals, *, legacy_reader=False):
    """Normalize copies while each source still has its own tracks and canvas."""
    parent, internals = copy.deepcopy(parent), copy.deepcopy(internals)
    if legacy_reader:
        omit_legacy_placement_markers(parent, internals)
    payload = parent['payload']
    config = payload.get('config')
    if not isinstance(config, dict):
        raise CommandError('parent payload.config must be an object',
                           kind='invalid_revision', ref='parent', path=('payload', 'config'))
    separate = payload.get('clips', [])
    embedded = config.get('clips', [])
    if separate and embedded and separate != embedded:
        raise CommandError('parent clip collections disagree before placement normalization')
    config['clips'] = copy.deepcopy(separate or embedded)
    def media(row):
        return any(
            isinstance(clip, dict) and clip.get('clipType', 'media') in ('media', 'image', 'video')
            for clip in row.get('clips', [])
        )

    needs_canvas = media(config) or any(media(row['payload']) for row in internals)
    if not needs_canvas:
        return parent, internals, []
    result = normalize_for_read(config, parent_canvas=True)
    canvas(result['config'], 'parent timeline')
    payload['config'] = result['config']
    if separate:
        payload['clips'] = copy.deepcopy(result['config']['clips'])
    findings = [{'owner': 'parent', **item} for item in result['findings']]
    for row in internals:
        child = row['payload']
        if not media(child):
            continue
        try:
            canonical, alias_findings = canonicalize_rect_aliases(child)
            normalized = package_query({'operation': 'normalize', 'config': canonical})
            if not any(finding.get('kind') == 'canvas-not-required' for finding in normalized['findings']):
                canvas(normalized['config'], f"internal timeline {row.get('revision_id')}")
        except (ValueError, CommandError) as exc:
            clips = child.get('clips', [])
            first = next((clip for clip in clips if isinstance(clip, dict)
                          and clip.get('clipType', 'media') in ('media', 'image', 'video')), {})
            raise CommandError(f"internal timeline {row.get('revision_id', '?')!r} "
                               f"clip {first.get('id', '?')!r}: {exc}") from exc
        row['payload'] = normalized['config']
        findings.extend({'owner': row.get('revision_id'), **item} for item in alias_findings + normalized['findings'])
    return parent, internals, findings
