"""Document-owned authored music sources and their ordinary clip projection.

This module participates in the existing compiler and Clip methods. It neither
records operations nor owns transactions, retry state or a second undo stack.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from functools import wraps

from astrid.core.timeline import astrid_ns
from astrid.core.timeline.music_bed import canonical_basis, materialize as _materialize, move_point
from astrid.sdk.timeline._common import TimelineEditError


class MusicBedError(TimelineEditError):
    """Invalid ownership, supplied materialization or generated-span edit."""


def materialize(source, basis, owner, **kwargs):
    from astrid.sdk import timeline_intent as intent
    return _materialize(source, basis, owner, bars=intent.beat_sources(source, 'downbeat'),
                        beats=intent.beat_sources(source), **kwargs)


def collection(tl, *, create=False):
    config = (tl.bundle.get('parent') or {}).get('config') or {}
    app = config.get('app') if isinstance(config, Mapping) else None
    ns = app.get('astrid') if isinstance(app, Mapping) else None
    beds = ns.get('music_beds', {}) if isinstance(ns, Mapping) else {}
    if not isinstance(beds, dict):
        raise MusicBedError('app.astrid.music_beds must be an object')
    if create:
        config = tl.bundle.setdefault('parent', {}).setdefault('config', {})
        ns = astrid_ns.film_ns(config)
        ns['v'] = max(ns.get('v', 0), 2)
        beds = ns.setdefault('music_beds', {})
    return beds


def locate(tl, owner, source):
    from astrid.sdk.timeline.model import Clip

    # The released SDK has local clip/cut.layer addresses and physically shared
    # shot clips. Refuse ambiguous scopes rather than invent a second encoding.
    hits = []
    for row in tl.bundle.get('placements') or []:
        sid = str(row.get('shot_id'))
        candidate = Clip(tl, sid, source)
        if candidate.id == owner or candidate.address == owner:
            clips = tl._internal(sid)['clips']
            declared = collection(tl).get(owner, {}).get('outputs', [])
            if any(c.get('id') == source['id'] or c.get('id') in declared for c in clips):
                hits.append((sid, clips))
    if len(hits) != 1:
        raise MusicBedError(f'{owner}: authored music requires an unambiguous existing scoped address; '
                            'repeated physical shot occurrences require occurrence-local projection')
    return hits[0]


def entry_for(clip):
    for owner, entry in collection(clip._tl).items():
        if not isinstance(entry, dict):
            raise MusicBedError(f'{owner}: malformed music bed')
        source = entry.get('source') or {}
        if not isinstance(source, dict) or not isinstance(entry.get('outputs'), list):
            raise MusicBedError(f'{owner}: malformed music bed')
        if clip.id == source.get('id') or clip.id in entry.get('outputs', []):
            sid, _ = locate(clip._tl, owner, source)
            if sid == clip.shot_id:
                return owner, entry
    return None


def validate(tl):
    claimed = set()
    for owner, entry in collection(tl).items():
        if not isinstance(entry, dict) or set(entry) != {'owner', 'source', 'ripple_basis', 'outputs'} \
                or entry['owner'] != owner or not isinstance(entry['source'], dict) \
                or not isinstance(entry['source'].get('id'), str) \
                or not isinstance(entry['ripple_basis'], list) or not isinstance(entry['outputs'], list) \
                or any(not isinstance(x, str) for x in entry['outputs']):
            raise MusicBedError(f'{owner}: malformed music bed carrier')
        try:
            basis = canonical_basis(entry['ripple_basis'])
            if basis != entry['ripple_basis']:
                raise ValueError('ripple_basis is not canonical')
            sid, clips = locate(tl, owner, entry['source'])
            expected, _, _ = materialize(entry['source'], basis, owner, start=tl._shot_start(sid), fps=tl.fps)
        except (ValueError, TypeError, KeyError) as exc:
            raise MusicBedError(f'{owner}: malformed music bed: {exc}') from None
        ids = [c['id'] for c in expected]
        if ids != entry['outputs']:
            raise MusicBedError(f'{owner}: outputs disagree with authored music; edit the authored source via tl.clip({owner!r})')
        for item in expected:
            key = sid, item['id']
            if key in claimed:
                raise MusicBedError(f'{owner}: music output ownership collision: {item["id"]}')
            claimed.add(key)
            found = [c for c in clips if c.get('id') == item['id']]
            # Missing derived clips can be rebuilt. Supplied edits cannot be lost.
            if len(found) > 1 or (found and found[0] != item):
                raise MusicBedError(f'{owner}: inconsistent supplied music output {item["id"]}; '
                                    f'edit the authored source via tl.clip({owner!r})')


def validate_bundle(bundle):
    from astrid.sdk.timeline.checkout import Checkout
    validate(Checkout(bundle))


def dematerialize(tl, only=None):
    """Use authored sources for dependency/beat evaluation, never output pieces."""
    tl._music_output_owners = {}
    for owner, entry in collection(tl).items():
        if only is not None and owner != only:
            continue
        sid, clips = locate(tl, owner, entry['source'])
        old = [c for c in clips if c.get('id') in entry['outputs']]
        for c in old:
            tl._music_output_owners[id(c)] = owner
        index = min((clips.index(c) for c in old), default=len(clips))
        clips[:] = [c for c in clips if c.get('id') not in entry['outputs']]
        clips.insert(index, entry['source'])


def project(tl):
    for owner, entry in collection(tl).items():
        sid, clips = locate(tl, owner, entry['source'])
        try:
            output, _, _ = materialize(entry['source'], entry['ripple_basis'], owner,
                                        start=tl._shot_start(sid), fps=tl.fps)
        except (ValueError, TypeError) as exc:
            raise MusicBedError(f'{owner}: {exc}') from None
        # Source/output mapping is explicit; names never confer ownership.
        old = [c for c in clips if c is entry['source'] or c.get('id') in entry['outputs']]
        taken = {c.get('id') for c in clips if not any(c is x for x in old)}
        if any(c['id'] in taken for c in output):
            raise MusicBedError(f'{owner}: deterministic music output ID collision')
        index = min((clips.index(c) for c in old), default=len(clips))
        clips[:] = [c for c in clips if not any(c is x for x in old)]
        clips[index:index] = output
        entry['outputs'] = [c['id'] for c in output]


def managed_source(tl, data):
    return any(entry['source'] is data for entry in collection(tl).values())


def transition(tl, a, b, delta, skip_ids):
    """Transform current inputs once, at the evaluator's real boundary change."""
    from astrid.sdk.timeline.model import Clip
    from astrid.sdk import timeline_intent as intent

    keep_end = tl._scope is not None and tl._music == 'keep_end'
    if abs(delta) < 1e-9:
        return
    skipped = {owner for identity, owner in getattr(tl, '_music_output_owners', {}).items() if identity in skip_ids}
    beds = collection(tl)
    # Save intact pre-edit sources only for this open scope. This temporary
    # compiler state disappears when the scope closes; it is never serialized.
    pending = getattr(tl, '_music_pending', {})
    if keep_end:
        for clip in tl.clips():
            if not tl._is_music(clip.shot_id, clip.data) or managed_source(tl, clip.data):
                continue
            siblings = tl._internal(clip.shot_id)['clips']
            if intent._app(clip.data).get('split_from') or any(intent._app(c).get('split_from') == clip.id for c in siblings):
                continue  # already segmented legacy: never fabricate its source
            if clip.start < a < clip.end and id(clip.data) not in skip_ids:
                locate(tl, clip.id, clip.data)  # stable existing ID selector; refuse ambiguous scope
                from astrid.sdk.timeline.doc import _timeline_stamp
                snapshot = copy.deepcopy(clip.data)
                if _timeline_stamp(snapshot.get('_timeline')):
                    snapshot.pop('_timeline')
                pending.setdefault(clip.id, (clip.shot_id, snapshot, clip.data))
        tl._music_pending = pending
    for owner, entry in beds.items():
        sid, _ = locate(tl, owner, entry['source'])
        source = entry['source']
        old_shot = tl._shot_start(sid)
        old_start = old_shot + float(source.get('at') or 0)
        from astrid.core.timeline.cuts import clip_duration
        end = old_start + clip_duration(source) + sum(p['delta_s'] for p in entry['ripple_basis'])
        excluded = owner in skipped or id(source) in skip_ids
        if keep_end and not excluded and delta < 0 and a <= old_start and b >= end:
            raise MusicBedError(f'{owner}: this SDK cannot address a bed with no materialized outputs '
                                'without the missing occurrence-scoped owner interface; '
                                f'explicit source removal uses tl.clip({owner!r}).remove()')
        points = entry['ripple_basis']
        # plays_on leaves both the current audio mapping and basis unchanged.
        if keep_end and not excluded:
            points = [dict(at_s=move_point(p['at_s'], a, b, delta), delta_s=p['delta_s']) for p in points]
            if old_start < a < end:
                points.append(dict(at_s=a, delta_s=delta))
            elif delta < 0 and a <= old_start < b:
                points.append(dict(at_s=a, delta_s=-(b - old_start)))
            entry['ripple_basis'] = canonical_basis(points)
        new_shot = old_shot
        if delta > 0 and old_shot >= a - 1e-6:
            new_shot += delta
        elif delta < 0:
            new_shot = move_point(old_shot, a, b, delta)
        new_start = old_start
        if keep_end and not excluded:
            if delta < 0 and old_start >= a - 1e-6:
                new_start = max(a, old_start + delta)
            elif delta > 0 and old_start >= a - 1e-6:
                new_start += delta
        source['at'] = round(new_start - new_shot, 6)


def enroll(tl, records):
    beds = collection(tl)
    notes = []
    for owner, (sid, source, original_data) in getattr(tl, '_music_pending', {}).items():
        if owner in beds:
            continue
        basis = canonical_basis([dict(at_s=p, delta_s=d) for p, d, skip in records if id(original_data) not in skip])
        output, report, changed = materialize(source, basis, owner, start=tl._shot_start(sid), fps=tl.fps)
        if not changed:
            continue
        clips = tl._internal(sid)['clips']
        original = [c for c in clips if c.get('id') == source['id']]
        if len(original) != 1:
            raise MusicBedError(f'{owner}: source disappeared before music enrollment')
        if any(c.get('id') in {p['id'] for p in output} and c is not original[0] for c in clips):
            raise MusicBedError(f'{owner}: deterministic music output ID collision')
        beds = collection(tl, create=True)
        beds[owner] = dict(owner=owner, source=source, ripple_basis=basis, outputs=[source['id']])
        # Keep source in the working list until the compiler emits the plan.
        source['at'] = original[0].get('at', 0)
        original[0].clear()
        original[0].update(source)
        beds[owner]['source'] = original[0]
        notes.extend(report)
    return notes


def source_edit(method):
    """Route existing Clip operations to authored intent before runner registration."""
    @wraps(method)
    def call(clip, *args, **kwargs):
        found = entry_for(clip)
        if found is None:
            return method(clip, *args, **kwargs)
        if managed_source(clip._tl, clip.data) and any(c is clip.data for c in clip._tl._internal(clip.shot_id)['clips']):
            return method(clip, *args, **kwargs)
        owner, entry = found
        if clip.id != entry['source']['id']:
            raise MusicBedError(f'{clip.id}: generated music span is read-only; edit authored source via tl.clip({owner!r})')
        validate(clip._tl)
        from astrid.sdk.timeline.model import Clip
        tl = clip._tl
        sid, clips = locate(tl, owner, entry['source'])
        old_start = float(entry['source'].get('at') or 0)
        dematerialize(tl, only=owner)
        # Existing methods may resolve immediately. Temporarily ordinary source
        # evaluation avoids treating an in-progress authored edit as external drift.
        del collection(tl)[owner]
        authored = Clip(tl, sid, entry['source'])
        result = method(authored, *args, **kwargs)
        if any(c is authored.data for c in clips):
            entry['source'] = authored.data
            shift = float(authored.data.get('at') or 0) - old_start
            if shift:
                entry['ripple_basis'] = canonical_basis([dict(at_s=p['at_s'] + shift, delta_s=p['delta_s']) for p in entry['ripple_basis']])
            collection(tl)[owner] = entry
        project(tl)
        return clip if result is authored else result
    return call
