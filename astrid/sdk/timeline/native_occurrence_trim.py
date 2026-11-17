"""Trim one edge of an occurrence window; the content stays anchored in timeline time.

args are {occurrence_id, edge: 'start'|'end', to_ms}: the new absolute position of that edge.
The handler derives every stored field so the anchor cannot be broken by a caller:
  end edge:   duration = to - start
  start edge: delta = to - old start; start = to; source_offset += delta; duration -= delta
source_offset is the window origin into the shot's timeline (see core.timeline.occurrence_window),
so the end and every child's timeline position stay fixed.

Only what this trim changes is judged. An occurrence that already overran its content or touched
its neighbour keeps that finding; refusal needs an edge to be pushed further out.
"""
from __future__ import annotations

from astrid.core.timeline.occurrence_window import MIN_OCCURRENCE_DURATION_MS

from ._command_state import CommandError
from ._occurrence_window import content_extent_ms, occurrence_offset_ms
from ._recording import active
from .capture import CLIP_FIELDS, MAP_FIELDS, number, occurrence_start
from .native_occurrence_duplicate import OCCURRENCE_INTENT

_EPS = 1e-6
_LANE_FIELDS = (('placement', 'start_ms'), ('at_ms',), ('duration_ms',), ('track',),
                ('placement', 'track'), ('enabled',), ('active',), ('disabled',), ('hidden',),
                ('deleted',))


def occurrence_trim(ctx, command):
    owner, args = command['select'], command['args']
    if args['occurrence_id'] != owner.removeprefix('occurrence:'):
        raise CommandError('trim args name a different occurrence than the selection',
                           kind='identity_conflict', ref=owner)
    ctx.select('collection.occurrences', 'parent')
    if ctx.state.entity(owner, optional=True) is None:
        raise CommandError('no such occurrence to trim', kind='identity_conflict', ref=owner)
    ctx.read(owner, ['occurrence_id'])
    ctx.read(owner + '/shot', ['shot_id'])
    for path in MAP_FIELDS:
        ctx.read(owner, path)
    row = ctx.state.occurrence(owner)
    if (not active(row) or isinstance(row.get('speed'), bool) or row.get('speed') not in (None, 1)
            or any(row.get(key) is not None for key in ('time_map', 'timeMap'))):
        raise CommandError('trim requires an active translation-only occurrence', kind='unsupported_map', ref=owner)

    start = occurrence_start(row) * 1000
    offset = occurrence_offset_ms(row)
    duration = number(row.get('duration_ms'), 'occurrence.duration_ms', positive=True)
    end, to = start + duration, args['to_ms']
    if args['edge'] == 'end':
        new_start, new_offset, new_duration = start, offset, to - start
    else:
        delta = to - start
        new_start, new_offset, new_duration = to, offset + delta, duration - delta

    if new_offset < 0:
        raise CommandError('the start edge cannot reveal content before the shot begins',
                           kind='occurrence_boundary', ref=owner)
    if new_duration < MIN_OCCURRENCE_DURATION_MS and new_duration != duration:
        raise CommandError(f'trim must keep at least {MIN_OCCURRENCE_DURATION_MS} ms',
                           kind='unsupported_timing', ref=owner)

    # The window may not run past the shot's content, unless it already did and is not pushed further.
    if new_offset + new_duration > offset + duration + _EPS:
        for ref in ctx.select('collection.clips', owner):
            for field in CLIP_FIELDS:
                ctx.read(ref, [field])
        extent = content_extent_ms(ctx.state, owner)
        if new_offset + new_duration > extent + _EPS:
            raise CommandError(
                f'trim window ends at {new_offset + new_duration:g} ms but the shot has {extent:g} ms of content',
                kind='occurrence_boundary', ref=owner)

    lane = (row.get('placement') or {}).get('track', row.get('track'))
    if isinstance(lane, str):
        for field in ('id', 'kind', 'role', 'enabled', 'active', 'disabled', 'hidden', 'deleted'):
            ctx.read('parent/track:' + lane, [field])
    new_end = new_start + new_duration
    for other in ctx.select('move.blockers', owner):
        if other == owner:
            continue
        for path in _LANE_FIELDS:
            ctx.read(other, path)
        other_row = ctx.state.occurrence(other)
        other_start = occurrence_start(other_row) * 1000
        other_end = other_start + number(other_row.get('duration_ms'), 'occurrence.duration_ms', positive=True)
        if other_start > start and new_end > other_start + _EPS and new_end > end + _EPS:
            raise CommandError('trimmed interval crosses the next occurrence', kind='occurrence_boundary', ref=owner)
        if other_start < start and new_start < other_end - _EPS and new_start < start - _EPS:
            raise CommandError('trimmed interval crosses the previous occurrence', kind='occurrence_boundary', ref=owner)

    if args['edge'] == 'start':
        placement = row.get('placement')
        has_placement = isinstance(placement, dict) and 'start_ms' in placement
        if not has_placement and 'at_ms' not in row:
            raise CommandError('trim requires an authored occurrence start', kind='unsupported_timing', ref=owner)
        if has_placement:
            ctx.write(owner, ['placement', 'start_ms'], new_start)
        if 'at_ms' in row:
            ctx.write(owner, ['at_ms'], new_start)
        ctx.write(owner, ['source_offset'], new_offset)
    ctx.write(owner, ['duration_ms'], new_duration)


def capture_occurrence_trim(bundle, occurrence_id, *, edge, to_ms):
    """Capture on the original bundle; to_ms is the edge's new absolute position in ms."""
    from .commands import execute

    return execute(bundle, [{'schema_version': 1, 'op': 'occurrence.trim', 'version': '1',
                             'select': 'occurrence:' + occurrence_id,
                             'args': {'occurrence_id': occurrence_id, 'edge': edge, 'to_ms': to_ms},
                             'intent': dict(OCCURRENCE_INTENT)}]).commands[0]
