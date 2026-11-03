"""Bounded source trim of literal parent media, without refreshing media."""
from __future__ import annotations

import copy
import re

from astrid.sdk import timeline_intent as intent

from ._command_state import CommandError
from ._common import TimelineEditError
from .capture import CLIP_FIELDS, RELATIONS, number
from .units import trim_source_interval


def clip_trim(ctx, command):
    ref = command['select']
    if not isinstance(ref, str) or not re.fullmatch(r'parent/clip:[^/]+', ref) or '::' in ref:
        raise CommandError('clip.trim v1 requires a literal parent clip identity')
    if ctx.read(ref, ['id']) is None:
        raise CommandError('trim target disappeared', kind='identity_conflict', ref=ref)
    fields = (*CLIP_FIELDS, 'sourceURL', 'sourceUrl', 'source_url', 'src', 'source_uuid',
              'source', 'source_window', 'source_offset', 'anchor', 'animation', 'animations', 'keyframes', 'curves')
    clip = {key: ctx.read(ref, [key]) for key in fields}
    for key in ('at_ms', 'from_ms', 'to_ms', 'duration', 'duration_ms', 'time_map', 'timeMap',
                'clips', 'children', *fields[len(CLIP_FIELDS):]):
        if clip[key] is not None:
            raise CommandError(f'clip.trim v1 does not support {key}', kind='unsupported_timing', ref=ref)
    for key in ('animation', 'animations', 'keyframes', 'curves'):
        if ctx.read(ref, ['params', key]) is not None:
            raise CommandError(f'clip.trim v1 does not support params.{key}', kind='unsupported_timing', ref=ref)
    app = ctx.read(ref, ['app'])
    if app is not None and not isinstance(app, dict):
        raise CommandError('trim intent container must be object or null', ref=ref)
    nested = app.get('astrid') if isinstance(app, dict) else None
    if nested is not None and not isinstance(nested, dict):
        raise CommandError('trim intent container must be object or null', ref=ref)
    for prefix, container in (([], clip), (['app'], app), (['app', 'astrid'], nested)):
        for key in (*RELATIONS, 'anchor', 'standin', 'standin_intent', 'animation', 'animations'):
            value = ctx.read(ref, prefix + [key]) if not prefix else (container or {}).get(key)
            if key == 'fixed_start' and value in (None, True):
                continue
            if key == 'for' and prefix and value is not None:
                number(value, 'for', positive=True)
            elif value is not None:
                raise CommandError(f'clip.trim v1 does not support {key}', kind='unsupported_timing', ref=ref)
    if clip['clipType'] not in (None, 'media') or clip['clip_type'] not in (None, 'media'):
        raise CommandError('clip.trim applies to media clips', ref=ref)
    asset = clip['asset']
    if not isinstance(asset, str) or not asset or any(clip[key] is not None for key in
            ('asset_id', 'media_id', 'object_id')):
        raise CommandError('clip.trim requires the current registry asset identity', ref=ref)
    start = number(clip['at'], 'clip.at')
    if start < 0:
        raise CommandError('clip.trim requires a non-negative literal start', ref=ref)
    if clip['hold'] is not None:
        number(clip['hold'], 'clip.hold', positive=True)
    speed = 1 if clip['speed'] is None else clip['speed']
    number(speed, 'clip.speed', positive=True)
    source = {key: ctx.read('parent/registry', ['assets', asset, key]) for key in
              ('media_id', 'object_id', 'digest', 'content_digest', 'type', 'duration',
               'duration_ms', 'duration_seconds', 'source', 'source_window', 'time_map', 'timeMap')}
    if source['type'] == 'image':
        raise CommandError('cannot source-trim an image: images have no source window',
                           kind='unsupported_media', ref=ref)
    if any(source[key] is not None for key in ('source', 'source_window', 'time_map', 'timeMap')):
        raise CommandError('clip.trim v1 does not support a mapped media source', kind='unsupported_map', ref=ref)
    limit = source['duration']
    number(limit, 'registry source duration', positive=True)
    endpoints = []
    for key, arg, default in (('from', 'in_s', 0), ('to', 'out_s', limit)):
        current = default if clip[key] is None else clip[key]
        number(current, 'clip.' + key)
        value = command['args'].get(arg)
        endpoints.append(current if value is None else value)
    source_in, source_out = endpoints
    if source_in < 0 or source_out > limit or source_out <= source_in:
        raise CommandError(f'source range must fit inside the file (0–{limit:g} s)', ref=ref)
    # ``hold`` is authored source time; playback duration applies ``speed``.
    try:
        source_in, source_out, hold = trim_source_interval(source_in, source_out, limit)
    except TimelineEditError as exc:
        raise CommandError(str(exc), ref=ref) from exc
    ctx.dependency('trim.source', 'entity', {'asset': asset, 'source': source})
    ctx.bind('trim.target', ref, dependencies=['trim.source'])
    for key, value in (('from', source_in), ('to', source_out), ('hold', hold)):
        ctx.write(ref, [key], value)
    # Main's clear helpers also tidy empty/null containers and handle legacy
    # flat intent precedence. Guard the app object they consume as one unit.
    cleared = {'app': copy.deepcopy(app)}
    intent.set_until(cleared, None)
    intent.set_for(cleared, None)
    if 'app' in cleared:
        ctx.write(ref, ['app'], cleared['app'])
    else:
        ctx.clear(ref, ['app'])


def capture_clip_trim(bundle, select, *, in_s=None, out_s=None):
    """Capture original claims. Null endpoints retain the current/default bound."""
    from .commands import execute

    return execute(bundle, [{'schema_version': 1, 'op': 'clip.trim', 'version': '1',
                             'select': select, 'args': {'in_s': in_s, 'out_s': out_s},
                             'intent': {}}]).commands[0]
