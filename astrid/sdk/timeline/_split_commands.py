"""Literal parent media splits, with caller-allocated stable right identities."""
from __future__ import annotations

import copy
import math

from astrid.core.timeline.duration import clip_end_frame, clip_start_frame, clip_timeline_duration

from ._command_state import CommandError, equal
from ._common import _media_kind
from .capture import RELATIONS, number, rate


def clip_split(ctx, command):
    ref = command['select']
    if not isinstance(ref, str) or not ref.startswith('parent/clip:'):
        raise CommandError('clip.split v1 requires a parent clip identity')
    members = ctx.select('collection.clips', 'parent')
    if ref not in members:
        raise CommandError('split target disappeared', kind='identity_conflict', ref=ref)
    row = ctx.read(ref)
    if row.get('clipType') != 'media' or not isinstance(row.get('asset'), str):
        raise CommandError('clip.split v1 requires registry-backed media', ref=ref)
    source = ctx.read('parent/registry', ['assets', row['asset']])
    if not isinstance(source, dict):
        raise CommandError('clip.split v1 requires a known media source', ref=ref)
    if _media_kind(source) not in ('image', 'video', 'audio'):
        raise CommandError('clip.split v1 requires a known media source', ref=ref)
    for key in ('clip_type', 'asset_id', 'media_id', 'object_id', 'at_ms', 'from_ms', 'to_ms',
                'duration', 'duration_ms', 'time_map', 'timeMap', 'clips', 'children',
                'transition', 'effects', 'animation', 'animations', 'keyframes',
                'enter', 'exit', 'continuous'):
        if row.get(key) is not None:
            raise CommandError(f'clip.split v1 does not support {key}', ref=ref)
    params = row.get('params')
    if isinstance(params, dict) and any(params.get(key) is not None for key in (
            'keyframes', 'animation', 'animations', 'effects', 'fade_in', 'fade_out', 'volume_curve',
            'fadeIn', 'fadeOut', 'fadeInCurve', 'fadeOutCurve')):
        raise CommandError('clip.split v1 does not support animated media parameters', ref=ref)
    literal_for = []
    for prefix in ([], ['app'], ['app', 'astrid']):
        container = row
        for key in prefix:
            container = container.get(key) if isinstance(container, dict) else None
        if container is None:
            continue
        if not isinstance(container, dict):
            raise CommandError('split intent container must be object or null', ref=ref)
        for key in (*RELATIONS, 'anchor', 'standin', 'standin_intent', 'words', 'beats'):
            value = container.get(key)
            if value is not None:
                if key == 'fixed_start' and value is True:
                    continue
                if key != 'for':
                    raise CommandError(f'clip.split v1 does not support {key}', ref=ref)
                literal_for.append((prefix + [key], number(value, 'for', positive=True)))
    # No anchor grammar is invented here. Capture recognized dependency sites
    # on existing neighbors; membership guards cover subsequently added rows.
    for other in members:
        if other == ref:
            continue
        for prefix in ([], ['app'], ['app', 'astrid']):
            for key in (*RELATIONS, 'anchor', 'standin', 'standin_intent'):
                ctx.read(other, prefix + [key])
        if ctx.read(other, ['transition']) is not None:
            raise CommandError('clip.split v1 does not support parent transitions', ref=other)
    start = number(row.get('at'), 'at')
    speed = 1 if row.get('speed') is None else number(row['speed'], 'speed', positive=True)
    window = row.get('from') is not None or row.get('to') is not None
    if window:
        lo = number(row.get('from'), 'from')
        hi = number(row.get('to'), 'to')
        if lo < 0 or hi <= lo:
            raise CommandError('split requires a positive source window', ref=ref)
        if row.get('hold') is not None and not math.isclose(
                number(row['hold'], 'hold', positive=True), hi - lo, abs_tol=1e-9):
            raise CommandError('split requires matching hold and source window', ref=ref)
    elif source['type'] != 'image':
        raise CommandError('moving media split requires an explicit source window', ref=ref)
    try:
        duration = clip_timeline_duration(row)
    except ValueError as exc:
        raise CommandError(str(exc), ref=ref) from exc
    if not math.isfinite(duration):
        raise CommandError('split requires a finite duration', ref=ref)
    if any(not math.isclose(value, duration, abs_tol=1e-9) for _, value in literal_for):
        raise CommandError('split requires matching literal for and duration', ref=ref)
    on = number(command['args']['on'], 'on')
    if start < 0 or not start < on < start + duration:
        raise CommandError('split position must be strictly inside the clip', ref=ref)
    left, right = copy.deepcopy(row), copy.deepcopy(row)
    right.update(id=command['args']['right_id'], at=on)
    consumed = (on - start) * speed
    if row.get('hold') is not None:
        left['hold'], right['hold'] = consumed, row['hold'] - consumed
    if window:
        left['to'] = right['from'] = lo + consumed
    for path, _ in literal_for:
        for half, length in ((left, on - start), (right, start + duration - on)):
            container = half
            for key in path[:-1]:
                container = container[key]
            container[path[-1]] = length
    try:
        fps = float(rate(ctx.read('parent', ['config', 'output', 'fps'])))
        if (clip_end_frame(left, fps) != clip_start_frame(right, fps)
                or clip_end_frame(right, fps) != clip_end_frame(row, fps)):
            raise CommandError('split must preserve half-open frame geometry', ref=ref)
    except (ValueError, OverflowError) as exc:
        raise CommandError(str(exc), ref=ref) from exc
    ctx.bind('split.target', ref)
    ctx.create('parent', 'clip', right, after=ref)
    for key in ('hold', 'to'):
        if key in left and (key not in row or not equal(row[key], left[key])):
            ctx.write(ref, [key], left[key])
    for path, _ in literal_for:
        ctx.write(ref, path, on - start)


def capture_clip_split(bundle, select, *, on, right_id):
    """Allocate right_id once outside capture; replay never allocates an ID."""
    from .commands import execute

    return execute(bundle, [{'schema_version': 1, 'op': 'clip.split', 'version': '1',
                             'select': select, 'args': {'on': on, 'right_id': right_id}}]).commands[0]
