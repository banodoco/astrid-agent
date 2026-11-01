"""First canonical shot and placement, with caller-reserved stable identities."""
from __future__ import annotations

import copy
from fractions import Fraction

from astrid.core.timeline.duration import clip_start_frame

from ._command_state import ABSENT, CommandError
from ._recording import active
from .capture import number, rate
from .native_occurrence_duplicate import OCCURRENCE_INTENT


def occurrence_create(ctx, command):
    if ctx.read('parent', ['config', 'pinnedShotGroups']) not in (None, []):
        raise CommandError('legacy groups require recorded migration', kind='migration_needed')
    args = command['args']
    ctx.select('collection.occurrences', 'parent')
    ctx.select('collection.tracks', 'parent')
    track = 'parent/track:' + args['track']
    lane = ctx.read(track)
    if (not isinstance(lane, dict) or lane.get('kind') != 'visual'
            or lane.get('role', 'output') != 'output' or not active(lane)):
        raise CommandError('birth requires an active parent visual output track', ref=track)
    ctx.bind('create.track', track)
    output = ctx.read('parent', ['config', 'output'])
    if ctx.state.get('parent', ['config', 'output']) == ABSENT:
        output = {}
    if not isinstance(output, dict):
        raise CommandError('birth requires object or absent parent output')
    fps = rate(output.get('fps'))
    on = number(args['on'], 'on')
    duration = number(args['duration'], 'duration', positive=True)
    try:
        frame = clip_start_frame({'at': on, 'hold': duration}, float(fps))
    except (ValueError, OverflowError) as exc:
        raise CommandError(str(exc), kind='unsupported_timing') from exc
    start_ms = float(Fraction(frame, 1) / fps) * 1000
    shot_id, occurrence_id = args['shot_id'], args['occurrence_id']
    ctx.create_entity('shot:' + shot_id, kind='shot', owner=None, value={
        'payload': {'name': args['name'], 'items': []},
        'internal_timeline': {'output': copy.deepcopy(output), 'tracks': [], 'clips': [],
                              'registry': {'assets': {}}},
    })
    ctx.create_entity('occurrence:' + occurrence_id, kind='occurrence', owner='parent', value={
        'occurrence_id': occurrence_id, 'shot_id': shot_id, 'track': args['track'],
        'placement': {'start_ms': start_ms, 'track': args['track']},
        'duration_ms': duration * 1000, 'source_offset': 0, 'speed': 1, 'gain': 1,
    })


def capture_occurrence_create(bundle, occurrence_id, shot_id, *, name, track, on, duration):
    """Capture on the original bundle; times are seconds and IDs are reserved once."""
    from .commands import execute

    return execute(bundle, [{'schema_version': 1, 'op': 'occurrence.create', 'version': '1',
                             'select': 'parent', 'args': {
                                 'occurrence_id': occurrence_id, 'shot_id': shot_id, 'name': name,
                                 'track': track, 'on': on, 'duration': duration},
                             'intent': OCCURRENCE_INTENT}]).commands[0]
