"""Completion auto-placement: a pure builder over existing placement/media birth.

Resolves the legacy "anchor-after" choice once, on the caller's original bundle,
and emits one ordinary command with a literal start. Nothing new is executed:
occurrence images use placement.create, videos use occurrence.add-media, and
parent completions use clip.add-media. The capture dict
names the anchor timing and the fallback it consumed. Resolution is never
re-run on replay, so replay and inverse are exact whatever happened to the anchor.

Occurrence timing inputs travel in `consumed`; parent timing inputs use the existing
read guards. Each birth records the values itself. A later relevant change conflicts
at replay instead of landing at a stale position. Rebuilding requires an explicitly
approved new capture; an exact retry retains the original request and result.

Start rule (frames at the pinned rational fps, intervals half-open). The intent saved
at submit time wins; a lane or time is recomputed only when none was saved:
  anchor present  -> the anchor's end frame (start + max(1, round(duration * fps))),
                     on preferred_track (else the explicit parent track or occurrence lane).
  anchor absent   -> the saved fallback_at (global seconds) on fallback_track (else the
                     explicit parent track or occurrence lane); with no saved time, the
                     lane end of that owner's clips, or the owner's start if empty.
Absent means no anchor id was given or no such raw clip exists in the explicit owner.
A present anchor with unusable timing refuses; it never falls back. A saved track that
no longer exists refuses (dependency_changed); it never moves to another lane.
Completion holds are distinct from the shared 4 s hold: image 5 s, video 5 s
unless the caller supplies the completed asset's duration.

auto_place_shared is the same builder for the App's shared-shot placement, over the
canonical owner a legacy group migrates to (one occurrence that exclusively owns its
shot; display IDs and unmigrated pinnedShotGroups never identify an owner):
  append    -> after the shot's greatest end frame over ALL its clips (not only one
               lane); an empty shot starts at the occurrence start.
  explicit  -> the clamped frame, shifted +1 while a clip START frame in the shot is
               equal (interval overlap is allowed).
  lane      -> the anchor's lane when the anchor is in the shot, else the occurrence lane.
  hold      -> 4 s for image and video; video authors the source window 0..4.
  pooled    -> shot.pool add (no clip); placing a media that is already pooled promotes it:
               the birth command plus shot.pool remove, in one batch.
All its reads (extent, starts, anchor lane) are guarded through every clip row of the shot.
"""
from __future__ import annotations

import math
from fractions import Fraction

from astrid.core.timeline.duration import clip_end_frame, clip_start_frame

from ._command_state import CommandError, State
from ._recording import Recording
from .capture import number, occurrence_start, rate
from .native_occurrence_media_commands import capture_occurrence_add_media
from .native_parent_media_commands import capture_clip_add_media
from .native_placement_commands import (
    _explicit_occurrence,
    _raw_clip_id,
    capture_placement_create,
    capture_placement_unplace,
)
from .native_shot_pool_commands import POOL, capture_shot_pool

COMPLETION_SECONDS = 5
SHARED_SECONDS = 4


def _end_frame(clip, origin, fps):
    at = clip['at'] if 'at' in clip else number(clip.get('at_ms'), 'clip.at_ms') / 1000
    try:
        return clip_end_frame({**clip, 'at': origin + number(at, 'clip.at')}, float(fps))
    except ValueError as exc:
        raise CommandError(str(exc), kind='unsupported_timing', ref=clip.get('id')) from exc


def _kind(media, verified_media):
    for value in verified_media:
        if value['selection'] == media:
            return str(value['media_type']).split('/')[0]
    raise CommandError('completed media is not verified', kind='dependency_unavailable')


def _birth(bundle, owner, clip_id, media, kind, lane, home, origin, fps, frame, length, ownership,
           verified_media, consumed):
    on = float(Fraction(frame, 1) / fps)
    if kind == 'image' and lane == home:
        return capture_placement_create(bundle, owner, clip_id, media, on, length, ownership=ownership,
                                        verified_media=verified_media, consumed=consumed), on
    number(length, 'duration', positive=True)
    clip = {'id': clip_id, 'clipType': kind, 'asset': media['asset'], 'track': lane,
            'at': on - origin, 'hold': length}
    if kind == 'video':
        clip.update({'from': 0, 'to': length, 'speed': 1})
    return capture_occurrence_add_media(bundle, owner, clip, media, verified_media=verified_media,
                                        consumed=consumed), on


def auto_place_completion(bundle, owner, clip_id, media, *, ownership, verified_media,
                          anchor=None, duration=None, preferred_track=None, fallback_at=None,
                          fallback_track=None, track=None):
    """Return {'commands': [...], 'capture': {...}} for one completed generation.

    owner is occurrence:<raw ID> with ownership='occurrence', or 'parent' with
    ownership='parent' and an explicit existing track. clip_id is the caller-reserved raw ID
    and media the exact five-field selection of the completed variant.
    preferred_track, fallback_at (global seconds) and fallback_track are the values
    saved when the task was submitted.
    """
    if owner == 'parent':
        if ownership != 'parent' or not isinstance(track, str) or not track:
            raise CommandError('parent completion requires explicit parent ownership and track',
                               kind='unsupported_ownership')
        return _parent_completion(bundle, clip_id, media, verified_media=verified_media,
                                  anchor=anchor, duration=duration, track=track,
                                  preferred_track=preferred_track, fallback_at=fallback_at,
                                  fallback_track=fallback_track)
    if track is not None:
        raise CommandError('parent track requires parent ownership', kind='unsupported_ownership')
    _explicit_occurrence(owner, ownership)
    _raw_clip_id(clip_id)
    kind = _kind(media, verified_media)
    if kind not in ('image', 'video'):
        raise CommandError('completion placement requires image or video media', kind='unsupported_media')
    state = State(bundle)
    row = state.occurrence(owner)
    # Both births re-check this at replay.
    if sum(1 for other in bundle['placements'] if other['shot_id'] == row['shot_id']) != 1:
        raise CommandError('shared authored placement is unsupported', kind='unsupported_ownership', ref=owner)
    origin = occurrence_start(row)
    home = (row.get('placement') or {}).get('track', row.get('track'))
    fps = rate(state.config('parent').get('output', {}).get('fps'))
    clips = state.collection(owner, 'clip')
    found = next((clip for clip in clips if anchor is not None and clip.get('id') == anchor), None)
    lane = (preferred_track if found is not None else fallback_track) or home
    if lane != home and state.entity(owner + '/track:' + str(lane), optional=True) is None:
        raise CommandError('saved placement track no longer exists', kind='dependency_changed', ref=owner)
    if found is not None:
        frame, resolution, consumed = _end_frame(found, origin, fps), 'anchor-after', [found['id']]
    elif fallback_at is not None:
        frame = clip_start_frame({'at': number(fallback_at, 'fallback_at'), 'hold': 1}, float(fps))
        resolution, consumed = 'fallback-at', []
    else:
        ends = [_end_frame(clip, origin, fps) for clip in clips if clip.get('track') == lane]
        frame = max(ends, default=math.floor(origin * fps + Fraction(1, 2)))
        resolution, consumed = 'lane-end', [clip['id'] for clip in clips]
    length = COMPLETION_SECONDS if kind == 'image' or duration is None else duration
    command, on = _birth(bundle, owner, clip_id, media, kind, lane, home, origin, fps, frame, length,
                         ownership, verified_media, consumed)
    return {'commands': [command],
            'capture': {'owner': owner, 'clip': clip_id, 'kind': kind, 'media': dict(media),
                        'anchor': {'id': anchor, 'state': 'present' if found is not None else 'absent'},
                        'resolution': resolution, 'lane': lane, 'consumed': consumed,
                        'fps': {'numerator': fps.numerator, 'denominator': fps.denominator},
                        'start_frame': frame, 'on': on, 'seconds': length}}


def _parent_completion(bundle, clip_id, media, *, verified_media, anchor, duration, track,
                       preferred_track, fallback_at, fallback_track):
    from .commands import execute

    _raw_clip_id(clip_id)
    if anchor is not None:
        _raw_clip_id(anchor)
    kind = _kind(media, verified_media)
    if kind not in ('image', 'video'):
        raise CommandError('completion placement requires image or video media', kind='unsupported_media')
    # Query through the existing recorder. The literal birth re-observes these
    # constraints in its own recorder; no policy is re-resolved during replay.
    query = Recording(bundle, {}, 0)
    fps = rate(query.read('parent', ['config', 'output', 'fps']))
    members = query.select('collection.clips', 'parent')
    found = 'parent/clip:' + anchor if anchor is not None else None
    found = found if found in members else None
    saved_track = preferred_track if found is not None else fallback_track
    lane = track if saved_track is None else saved_track
    if not isinstance(lane, str) or query.state.entity('parent/track:' + lane, optional=True) is None:
        raise CommandError('saved placement track no longer exists', kind='dependency_changed')

    def clip(ref):
        # Keep absent-vs-null states, including duration.py's default branches.
        row = {}
        for field in ('at', 'at_ms', 'track', 'hold', 'from', 'to', 'speed'):
            value = query.read(ref, [field])
            if query.state.get(ref, [field])['state'] == 'present':
                row[field] = value
        return row

    if found is not None:
        frame, resolution, consumed = _end_frame(clip(found), 0, fps), 'anchor-after', [anchor]
    elif fallback_at is not None:
        frame = clip_start_frame({'at': number(fallback_at, 'fallback_at'), 'hold': 1}, float(fps))
        resolution, consumed = 'fallback-at', []
    else:
        ends, consumed = [], []
        for ref in members:
            if query.read(ref, ['track']) == lane:
                ends.append(_end_frame(clip(ref), 0, fps))
                consumed.append(ref.split('/clip:', 1)[1])
        frame, resolution = max(ends, default=0), 'lane-end'
    on = float(Fraction(frame, 1) / fps)
    length = COMPLETION_SECONDS if kind == 'image' or duration is None else duration
    row = {'id': clip_id, 'clipType': kind, 'asset': media['asset'], 'track': lane,
           'at': on, 'hold': length}
    if kind == 'video':
        row.update({'from': 0, 'to': length, 'speed': 1})
    command = capture_clip_add_media(bundle, row, media, verified_media=verified_media)
    for read in query.record['reads']:
        if not any(item['ref'] == read['ref'] and item['path'] == read['path']
                   for item in command['reads']):
            command['reads'].append(read)
    command = execute(bundle, [command], verified_media=verified_media).commands[0]
    return {'commands': [command],
            'capture': {'owner': 'parent', 'clip': clip_id, 'kind': kind, 'media': dict(media),
                        'anchor': {'id': anchor, 'state': 'present' if found else 'absent'},
                        'resolution': resolution, 'lane': lane, 'consumed': consumed,
                        'fps': {'numerator': fps.numerator, 'denominator': fps.denominator},
                        'start_frame': frame, 'on': on, 'seconds': length}}


def auto_place_shared(bundle, owner, clip_id, media, *, ownership, verified_media,
                      frame=None, anchor=None, pooled=False):
    """Return {'commands': [...], 'capture': {...}} for one media placed into a shared shot.

    owner is the canonical occurrence that exclusively owns the shot. frame=None appends;
    an integer is an explicit global frame (clamped at 0, shifted while a start is taken).
    """
    _explicit_occurrence(owner, ownership)
    _raw_clip_id(clip_id)
    kind = _kind(media, verified_media)
    if kind not in ('image', 'video'):
        raise CommandError('shared placement requires image or video media', kind='unsupported_media')
    state = State(bundle)
    row = state.occurrence(owner)
    if sum(1 for other in bundle['placements'] if other['shot_id'] == row['shot_id']) != 1:
        raise CommandError('shared authored placement is unsupported', kind='unsupported_ownership', ref=owner)
    if state.config('parent').get('pinnedShotGroups') not in (None, []):
        raise CommandError('legacy groups require recorded migration', kind='migration_needed', ref=owner)
    if pooled:
        if frame is not None or anchor is not None:
            raise CommandError('pooled media has no frame or anchor', kind='unsupported_timing')
        command = capture_shot_pool(bundle, owner, 'add', media, ownership=ownership,
                                    verified_media=verified_media)
        return {'commands': [command],
                'capture': {'owner': owner, 'clip': clip_id, 'kind': kind, 'media': dict(media),
                            'resolution': 'pooled'}}
    origin = occurrence_start(row)
    fps = rate(state.config('parent').get('output', {}).get('fps'))
    clips = state.collection(owner, 'clip')
    found = next((clip for clip in clips if anchor is not None and clip.get('id') == anchor), None)
    home = (row.get('placement') or {}).get('track', row.get('track'))
    lane = found.get('track') if found is not None else home
    consumed = [clip['id'] for clip in clips]
    capture = {'owner': owner, 'clip': clip_id, 'kind': kind, 'media': dict(media),
               'anchor': {'id': anchor, 'state': 'present' if found is not None else 'absent'},
               'lane': lane, 'consumed': consumed,
               'fps': {'numerator': fps.numerator, 'denominator': fps.denominator}}
    if frame is None:
        start = max((_end_frame(clip, origin, fps) for clip in clips), default=math.floor(origin * fps + Fraction(1, 2)))
        capture.update(resolution='append', extent_frame=start)
    else:
        if isinstance(frame, bool) or not isinstance(frame, int):
            raise CommandError('frame must be an integer', kind='unsupported_timing')
        taken = {clip_start_frame({**clip, 'at': origin + number(clip.get('at'), 'clip.at')}, float(fps))
                 for clip in clips}
        start, skipped = max(frame, 0), []
        while start in taken:
            skipped.append(start)
            start += 1
        capture.update(resolution='explicit', requested_frame=frame, skipped=skipped)
    command, on = _birth(bundle, owner, clip_id, media, kind, lane, home, origin, fps, start, SHARED_SECONDS,
                         ownership, verified_media, consumed)
    capture.update(start_frame=start, on=on, seconds=SHARED_SECONDS)
    commands = [command]
    pool = state.get(owner + '/source', POOL).get('value')
    if isinstance(pool, list) and media['asset'] in pool:
        from .commands import execute

        born = execute(bundle, commands, verified_media=verified_media).bundle
        commands.append(capture_shot_pool(born, owner, 'remove', media, ownership=ownership,
                                          verified_media=verified_media))
        capture['promoted'] = True
    return {'commands': commands, 'capture': capture}


def unplace_shared(bundle, owner, clip_id, *, ownership, keep_as_pooled=False, media=None,
                   verified_media=()):
    """Remove one shared placement; its media binding is kept.

    keep_as_pooled also adds the media to the shot pool, so it needs the exact five-field
    selection of the clip's media and its verification.
    """
    unplace = capture_placement_unplace(bundle, owner, clip_id, ownership=ownership)
    if not keep_as_pooled:
        return {'commands': [unplace]}
    from .commands import execute

    if media is None or State(bundle).entity(owner + '/clip:' + clip_id).get('asset') != media['asset']:
        raise CommandError('keepAsPooled requires the exact selection of the placed media',
                           kind='identity_conflict', ref=owner)
    removed = execute(bundle, [unplace]).bundle
    return {'commands': [unplace, capture_shot_pool(removed, owner, 'add', media, ownership=ownership,
                                                    verified_media=verified_media)]}
