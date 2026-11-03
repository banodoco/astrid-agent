"""Built-in move recipe and original-state capture, shared by every frontend."""
from __future__ import annotations

import copy
import math
from fractions import Fraction

from astrid.core.timeline.duration import clip_start_frame, clip_timeline_duration

from ._command_state import CommandError

MOVE_INTENT = {"ripple": "none", "anchor": "fixed", "position": "absolute"}
CLIP_FIELDS = ('id', 'at', 'at_ms', 'track', 'asset', 'asset_id', 'media_id', 'object_id',
               'clipType', 'clip_type', 'enabled', 'active', 'disabled', 'hidden', 'deleted',
               'from', 'to', 'from_ms', 'to_ms', 'hold', 'duration', 'duration_ms', 'speed', 'time_map', 'timeMap',
               'clips', 'children')
RELATIONS = ('on', 'fixed_start', 'until', 'until_film_end', 'for', 'cut', 'sequence',
             'sequence_index', 'sequence_fit', 'orphan', 'formulas')
MAP_FIELDS = (('placement', 'start_ms'), ('at_ms',), ('duration_ms',), ('source_offset',),
              ('speed',), ('track',), ('placement', 'track'), ('time_map',), ('timeMap',),
              ('enabled',), ('active',), ('disabled',), ('hidden',), ('deleted',))


def number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise CommandError(f"{label} must be a finite number", kind='unsupported_timing')
    try:
        result = float(value)
    except OverflowError as exc:
        raise CommandError(f"{label} must be finite", kind='unsupported_timing') from exc
    if not math.isfinite(result):
        raise CommandError(f"{label} must be finite", kind='unsupported_timing')
    if positive and result <= 0:
        raise CommandError(f"{label} must be positive", kind='unsupported_timing')
    return result


def rate(value):
    if value is None:
        return Fraction(30)
    if isinstance(value, dict) and set(value) == {'numerator', 'denominator'}:
        n, d = value['numerator'], value['denominator']
        if isinstance(n, bool) or isinstance(d, bool) or not isinstance(n, int) or not isinstance(d, int):
            raise CommandError("fps rational requires integer numerator/denominator")
        if n <= 0 or d <= 0:
            raise CommandError("fps rational must be positive")
        return Fraction(n, d)
    return Fraction(str(number(value, 'fps', positive=True)))


def _output_rate(ctx):
    """Read either current output-rate spelling without defaulting a rational away."""
    value = ctx.read('parent', ['config', 'output', 'fps'])
    if value is not None:
        return rate(value)
    output = ctx.read('parent', ['config', 'output'])
    if isinstance(output, dict) and set(output) == {'numerator', 'denominator'}:
        return rate(output)
    return rate(None)


def occurrence_start(row):
    placement = row.get('placement') or {}
    if not isinstance(placement, dict):
        raise CommandError('occurrence placement must be an object', kind='unsupported_map')
    start = placement.get('start_ms')
    fallback = row.get('at_ms')
    if start is not None and fallback is not None and not math.isclose(
            number(start, 'placement.start_ms'), number(fallback, 'at_ms'), abs_tol=1e-9):
        raise CommandError('contradictory occurrence start codecs', kind='unsupported_timing')
    return number(start if start is not None else (fallback if fallback is not None else 0),
                  'occurrence start') / 1000


def _duration(clip):
    speed = clip.get('speed')
    if speed is not None:
        number(speed, 'clip.speed', positive=True)
    if 'at' in clip and 'at_ms' in clip and not math.isclose(
            number(clip['at'], 'clip.at'), number(clip['at_ms'], 'clip.at_ms') / 1000,
            abs_tol=1e-9):
        raise CommandError("contradictory at/at_ms timing codecs", kind='unsupported_timing')
    value = dict(clip, at=clip.get('at', number(clip.get('at_ms', 0), 'at_ms') / 1000))
    if 'at' not in clip and 'at_ms' not in clip:
        raise CommandError('move requires an explicit authored start', kind='unsupported_timing')
    start = number(value['at'], 'clip start')
    if start < 0:
        raise CommandError('clip.move v1 does not support a clipped negative start', kind='unsupported_timing')
    lengths = []
    if isinstance(clip.get('hold'), (int, float)) and not isinstance(clip.get('hold'), bool):
        lengths.append(clip_timeline_duration(value))
    elif clip.get('from') is not None and clip.get('to') is not None:
        lengths.append(clip_timeline_duration(value))
    for field, scale in (('duration', 1), ('duration_ms', 1000)):
        if clip.get(field) is not None:
            projected = dict(value, hold=number(clip[field], field) / scale)
            lengths.append(clip_timeline_duration(projected))
    if not lengths or lengths[0] <= 0 or any(not math.isclose(x, lengths[0], abs_tol=1e-9)
                                            for x in lengths):
        raise CommandError("move needs an unambiguous positive authored duration/window",
                           kind='unsupported_timing')
    return lengths[0]


def _provenance(ctx, owner):
    bundle = ctx.state.bundle
    pin = {'parent': copy.deepcopy(bundle.get('base_parent', {}))}
    if owner != 'parent':
        shot_id = ctx.state.occurrence(owner)['shot_id']
        pin['source'] = copy.deepcopy(bundle.get('source_mapping', {}).get('shots', {}).get(shot_id, {}))
    ctx.dependency('move.images', 'entity', pin)


def move(ctx, command):
    ref = command['select']
    if not isinstance(ref, str) or '/clip:' not in ref or '::' in ref:
        raise CommandError("clip.move requires a scoped clip identity")
    owner = ref.rsplit('/', 1)[0]
    if owner != 'parent' and not owner.startswith('occurrence:'):
        raise CommandError("clip.move v1 requires parent or occurrence ownership")
    if ctx.select('move.target', owner, [ref]) != [ref]:
        raise CommandError("move target disappeared", kind='identity_conflict', ref=ref)
    for field in CLIP_FIELDS:
        ctx.read(ref, [field])
    clip = ctx.state.entity(ref)
    for key in ('time_map', 'timeMap', 'clips', 'children', 'from_ms', 'to_ms'):
        if clip.get(key) is not None:
            raise CommandError(f"clip.move v1 does not support {key}", kind='unsupported_map', ref=ref)
    for container in (['app'], ['app', 'astrid']):
        state = ctx.state.get(ref, container)
        if state['state'] == 'present' and isinstance(state['value'], dict):
            continue
        ctx.read(ref, container)
        if state.get('value') is not None:
            raise CommandError("intent container must be object or null", ref=ref, path=container)
    for prefix in (['app'], ['app', 'astrid']):
        for key in RELATIONS:
            value = ctx.read(ref, prefix + [key])
            if key in ('sequence', 'sequence_index', 'sequence_fit', 'formulas') and value is not None:
                if value != {}:
                    raise CommandError(f"clip.move v1 does not support {key}", kind='unsupported_timing', ref=ref)
    track = clip.get('track')
    if not isinstance(track, str) or not track:
        raise CommandError("move requires a referenced track", ref=ref)
    track_ref = owner + '/track:' + track
    for field in ('id', 'kind', 'role', 'enabled', 'active', 'disabled', 'hidden', 'deleted'):
        ctx.read(track_ref, [field])
    if ctx.state.entity(track_ref, optional=True) is None:
        raise CommandError("move track disappeared", kind='identity_conflict', ref=track_ref)
    ctx.bind('move.track', track_ref)
    ctx.bind('move.owner', owner)
    asset = clip.get('asset', clip.get('asset_id'))
    source_dependencies = []
    if asset is not None:
        registry_ref = owner + '/registry'
        source = {}
        for key in ('media_id', 'object_id', 'digest', 'content_digest', 'type', 'duration',
                    'duration_ms', 'duration_seconds', 'source'):
            source[key] = ctx.read(registry_ref, ['assets', str(asset), key])
        ctx.dependency('move.source', 'entity', {'ref': registry_ref, 'asset': asset, 'source': source})
        source_dependencies.append('move.source')
    fps = _output_rate(ctx)
    if owner != 'parent':
        child_fps = ctx.read(owner + '/source', ['output', 'fps'])
        # Expose consumed child rate via the occurrence config view, not a revision ID.
        if child_fps is not None and rate(child_fps) != fps:
            raise CommandError("parent and child fps differ", kind='unsupported_timing')
    ctx.dependency('move.quantization', 'time-map', {'numerator': fps.numerator,
                                                   'denominator': fps.denominator,
                                                   'rounding': 'half-toward-positive-infinity'})
    on = number(command['args']['on'], 'on')
    if on < 0:
        raise CommandError("on must not be negative")
    try:
        frame = clip_start_frame({'at': on, 'hold': 1}, float(fps))
    except (ValueError, OverflowError) as exc:
        raise CommandError(str(exc), kind='unsupported_timing', ref=ref) from exc
    quantized = Fraction(frame, 1) / fps
    try:
        duration = _duration(clip)
    except ValueError as exc:
        if isinstance(exc, CommandError):
            raise
        raise CommandError(str(exc), kind='unsupported_timing', ref=ref) from exc
    origin = 0.0
    if owner != 'parent':
        ctx.read(owner, ['occurrence_id'])
        ctx.read(owner + '/shot', ['shot_id'])
        row = ctx.state.occurrence(owner)
        for path in MAP_FIELDS:
            ctx.read(owner, path)
        for key in ('time_map', 'timeMap'):
            if row.get(key) is not None:
                raise CommandError(f"unsupported occurrence {key}", kind='unsupported_map', ref=owner)
        offset, speed = row.get('source_offset'), row.get('speed')
        if (isinstance(offset, bool) or isinstance(speed, bool)
                or offset not in (None, 0) or speed not in (None, 1)):
            raise CommandError("clip.move v1 requires a translation-only occurrence map",
                               kind='unsupported_map', ref=owner)
        origin = occurrence_start(row)
        lane = (row.get('placement') or {}).get('track', row.get('track'))
        if isinstance(lane, str):
            for field in ('id', 'kind', 'role', 'enabled', 'active', 'disabled', 'hidden', 'deleted'):
                ctx.read('parent/track:' + lane, [field])
        blockers = ctx.select('move.blockers', owner)
        next_start = math.inf
        for other in blockers:
            for path in (('placement', 'start_ms'), ('at_ms',), ('track',), ('placement', 'track'),
                         ('enabled',), ('active',), ('disabled',), ('hidden',), ('deleted',)):
                ctx.read(other, path)
            other_row = ctx.state.occurrence(other)
            start = occurrence_start(other_row)
            if other != owner and start > origin:
                next_start = min(next_start, start)
        if float(quantized) + duration > next_start + 1e-9:
            raise CommandError("moved interval crosses the next occurrence", kind='occurrence_boundary', ref=owner)
    local = float(quantized) - origin
    if local < 0:
        raise CommandError("moved interval precedes its occurrence", kind='occurrence_boundary', ref=owner)
    _provenance(ctx, owner)
    ctx.bind('move.target', ref, dependencies=['move.images', 'move.quantization', *source_dependencies])
    ctx.write(ref, ['at_ms'] if 'at_ms' in clip and 'at' not in clip else ['at'],
              local * 1000 if 'at_ms' in clip and 'at' not in clip else local)
    if 'at' in clip and 'at_ms' in clip:
        ctx.write(ref, ['at_ms'], local * 1000)
    ctx.object(ref, ['app'])
    ctx.object(ref, ['app', 'astrid'])
    for prefix in (['app'], ['app', 'astrid']):
        for key in ('on', 'until', 'until_film_end'):
            ctx.clear(ref, prefix + [key])
    ctx.write(ref, ['app', 'astrid', 'fixed_start'], True)
    ctx.write(ref, ['app', 'astrid', 'for'], duration)


def track_create(ctx, command):
    owner, row = command['select'], command['args']['row']
    ctx.create(owner, 'track', row)


def _capture(bundle, command):
    from .commands import execute
    return execute(bundle, [command]).commands[0]


def move_intent(select):
    intent = dict(MOVE_INTENT)
    if isinstance(select, str) and select.startswith('occurrence:'):
        intent['ownership'] = 'occurrence'
    return intent


def capture_move(bundle, select, on):
    """Capture on the original authored bundle; `on` is absolute global seconds."""
    return _capture(bundle, {'schema_version': 1, 'op': 'clip.move', 'version': '1',
                             'select': select, 'args': {'on': on}, 'intent': move_intent(select)})


def capture_track_create(bundle, owner, row):
    """Small structural proof: caller allocates the row ID once, before sending."""
    return _capture(bundle, {'schema_version': 1, 'op': 'track.create', 'version': '1',
                             'select': owner, 'args': {'row': copy.deepcopy(row)}, 'intent': {}})


def capture_clip_delete(bundle, select):
    """Capture a bounded parent removal through the authoritative executor."""
    return _capture(bundle, {'schema_version': 1, 'op': 'clip.delete', 'version': '1',
                             'select': select, 'args': {}, 'intent': {}})
