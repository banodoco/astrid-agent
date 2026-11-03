"""Explicit-owner placement using the existing absolute clip.move recipe.

This changes an existing clip's start, preserving its media and source window.
It does not implement the App's legacy generation placement/pooling algorithm.

placement.create adds an image; placement.unplace removes an occurrence-owned
image or video expression. They reuse media birth and bounded clip removal; there is
no pooling, auto-position, collision shifting or registry removal.
"""
from __future__ import annotations

import copy
import re
from fractions import Fraction

from astrid.core.timeline.duration import clip_start_frame

from ._command_state import CommandError
from ._delete_commands import _unreferenced
from ._media_inputs import _verified
from .capture import _duration, move, move_intent, number, occurrence_start, rate
from .native_occurrence_media_commands import _exclusive, _owner, add_media

PLACEMENT_INTENT = {'ownership': 'occurrence'}


def placement_move(ctx, command):
    ref = command['select']
    owner = ref.rsplit('/', 1)[0]
    if owner != 'parent':
        if ctx.select('placement.owners', owner) != [owner]:
            raise CommandError('shared authored placement is unsupported',
                               kind='unsupported_ownership', ref=owner)
    clip = ctx.state.entity(ref)
    asset = clip.get('asset', clip.get('asset_id'))
    if asset is not None:
        # Guard the entire existing binding, including opaque selection pins.
        # Read raw paths so accepted source writes also protect against ABA.
        if owner == 'parent':
            ctx.read('parent', ['registry', 'assets', str(asset)])
            ctx.read('parent', ['config', 'registry', 'assets', str(asset)])
        else:
            ctx.read(owner + '/source', ['registry', 'assets', str(asset)])
    move(ctx, command)
    if owner != 'parent':
        extent = number(ctx.read(owner, ['duration_ms']), 'occurrence.duration_ms', positive=True) / 1000
        moved = ctx.state.entity(ref)
        start = moved['at'] if 'at' in moved else moved['at_ms'] / 1000
        if start + _duration(moved) > extent + 1e-9:
            raise CommandError('placed interval exceeds occurrence duration',
                               kind='occurrence_boundary', ref=owner)


def capture_placement(bundle, owner, clip_id, on, *, ownership):
    """Capture global seconds on the original bundle, before local preview.

    owner is parent or occurrence:<raw ID>; clip_id is the raw child ID.
    ownership must explicitly match that scope. Shared authoring, pooling,
    automatic positioning, track changes and media selection are unsupported.
    The result is placement.move v1, with its authoritative capture.
    """
    if ownership == 'shared':
        raise CommandError('shared placement is unsupported', kind='unsupported_ownership')
    expected = ('parent' if owner == 'parent' else 'occurrence'
                if isinstance(owner, str) and re.fullmatch(r'occurrence:[^/]+', owner)
                and '::' not in owner else None)
    if expected is None or ownership != expected:
        raise CommandError('placement requires matching explicit parent or occurrence ownership',
                           kind='unsupported_ownership')
    if (not isinstance(clip_id, str) or not clip_id.strip()
            or '/' in clip_id or '::' in clip_id):
        raise CommandError('placement requires a raw clip identity', kind='identity_conflict')
    from .commands import execute

    ref = owner + '/clip:' + clip_id
    return execute(bundle, [{'schema_version': 1, 'op': 'placement.move', 'version': '1',
                             'select': ref, 'args': {'on': on},
                             'intent': move_intent(ref)}]).commands[0]


def placement_create(ctx, command):
    """Image-only expression at an absolute time; the wrapper adds no media semantics."""
    owner, _ = _owner(ctx, command['select'])
    _exclusive(ctx, owner)
    args = command['args']
    _, kind = _verified(ctx, args['media'])
    if kind != 'image':
        raise CommandError('placement.create v1 requires verified image media', kind='unsupported_media')
    row = ctx.state.occurrence(owner)
    lane = (row.get('placement') or {}).get('track', row.get('track'))
    if not isinstance(lane, str):
        raise CommandError('placement requires a referenced lane', kind='unsupported_timing', ref=owner)
    fps = rate(ctx.read('parent', ['config', 'output', 'fps']))
    frame = clip_start_frame({'at': number(args['on'], 'on'), 'hold': 1}, float(fps))
    at = float(Fraction(frame, 1) / fps) - occurrence_start(row)
    clip = {'id': args['id'], 'clipType': 'image', 'asset': args['media']['asset'], 'track': lane,
            'at': at, 'hold': args['hold']}
    add_media(ctx, {'select': owner, 'args': {'row': clip, 'media': args['media'],
                                                  'consumed': args.get('consumed', [])}})


def placement_unplace(ctx, command):
    """Remove one raw media expression; its exact row and registry binding survive undo."""
    ref = command['select']
    owner = ref.rsplit('/', 1)[0]
    _explicit_occurrence(owner, command.get('intent', {}).get('ownership'))
    _exclusive(ctx, owner)
    if ref not in ctx.select('collection.clips', owner):
        raise CommandError('placement target disappeared', kind='identity_conflict', ref=ref)
    row = ctx.read(ref)
    if row.get('clipType') not in ('media', 'image', 'video') or not isinstance(row.get('asset'), str):
        raise CommandError('unplace requires one placed media clip', kind='unsupported_media', ref=ref)
    if any(row.get(key) is not None for key in ('clips', 'children', 'time_map', 'timeMap')):
        raise CommandError('unplace does not support nested or mapped clips', kind='unsupported_map', ref=ref)
    # Guard the whole binding the removed expression pointed at, including ABA.
    ctx.read(owner + '/source', ['registry', 'assets', str(row['asset'])])
    _unreferenced(ctx)
    ctx.clear(ref, [])


def _explicit_occurrence(owner, ownership):
    if ownership != 'occurrence' or not isinstance(owner, str) or re.fullmatch(r'occurrence:(?!.*::)[^/]+', owner) is None:
        raise CommandError('placement requires explicit occurrence ownership', kind='unsupported_ownership')


def _raw_clip_id(clip_id):
    if not isinstance(clip_id, str) or not clip_id.strip() or '/' in clip_id or '::' in clip_id:
        raise CommandError('placement requires a raw clip identity', kind='identity_conflict')
    return clip_id


def capture_placement_create(bundle, owner, clip_id, media, on, hold, *, ownership, verified_media,
                             consumed=()):
    """Caller reserves clip_id and supplies the exact five-field Runtime media selection."""
    from .commands import execute

    _explicit_occurrence(owner, ownership)
    args = {'id': _raw_clip_id(clip_id), 'on': on, 'hold': hold, 'media': copy.deepcopy(media)}
    if consumed:
        args['consumed'] = list(consumed)
    return execute(bundle, [{'schema_version': 1, 'op': 'placement.create', 'version': '1',
                             'select': owner, 'args': args, 'intent': dict(PLACEMENT_INTENT)}], verified_media=verified_media).commands[0]


def capture_placement_unplace(bundle, owner, clip_id, *, ownership):
    from .commands import execute

    _explicit_occurrence(owner, ownership)
    return execute(bundle, [{'schema_version': 1, 'op': 'placement.unplace', 'version': '1',
                             'select': owner + '/clip:' + _raw_clip_id(clip_id), 'args': {},
                             'intent': dict(PLACEMENT_INTENT)}]).commands[0]
