"""Document-owned authored music sources and their ordinary clip projection.

This module participates in the existing compiler and Clip methods. It neither
records operations nor owns transactions, retry state or a second undo stack.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from contextlib import contextmanager
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


def prepare(tl):
    """Materialize shared placements with the canonical authoring-bundle copier.

    No pins are invented: an existing source mapping is copied when present.
    Legacy closure-less shared input must supply distinct occurrence identities.
    """
    from collections import Counter
    from astrid.core.timeline.authoring_bundle import (
        _authoring_shot_id, _copy_shot_for_placement, _remap_known_item_references,
    )

    rows = tl.bundle.get('placements') or []
    counts = Counter(str(row.get('shot_id')) for row in rows)
    shared = [row for row in rows if counts[str(row.get('shot_id'))] > 1]
    if not shared:
        return
    ids = [row.get('occurrence_id') for row in rows]
    if any(not isinstance(oid, str) or not oid for oid in ids) or len(set(ids)) != len(ids):
        raise MusicBedError('shared input requires unambiguous occurrence-local closure')
    # Preflight all copies before changing the caller's document.
    copies = []
    shots = tl.bundle['shots']
    mapping = tl.bundle.get('source_mapping')
    for row in shared:
        sid = str(row['shot_id'])
        new_sid = _authoring_shot_id(tl.bundle['project_id'], tl.bundle['timeline_id'],
                                    row['occurrence_id'], sid, shared=True)
        if new_sid in shots:
            raise MusicBedError(f'{new_sid}: authoring shot identity collision')
        shot = copy.deepcopy(shots[sid])
        item_ids = {}
        for field in ('payload', 'base_payload'):
            if field in shot:
                shot[field], field_item_ids = _copy_shot_for_placement(
                    shot[field], project_id=tl.bundle['project_id'], timeline_id=tl.bundle['timeline_id'],
                    occurrence_id=row['occurrence_id'], authoring_shot_id=new_sid, source_shot_id=sid)
                if field == 'payload':
                    item_ids = field_item_ids
                    for name in ('internal_timeline', 'base_internal_timeline'):
                        if name in shot:
                            shot[name] = _remap_known_item_references(shot[name], item_ids)
        shot['shot_id'] = new_sid
        mapped_source = None
        if mapping is not None:
            try:
                mapped_source = copy.deepcopy(mapping['shots'][sid])
                mapped_source['item_id_map'] = {old: item_ids.get(current, current)
                                                for old, current in mapped_source.get('item_id_map', {}).items()}
                mapping['placements'][row['occurrence_id']]['shot_id']
            except (KeyError, TypeError):
                raise MusicBedError('shared input has incomplete authoring source mapping') from None
        owned = {owner: _remap_known_item_references(entry, item_ids)
                 for owner, entry in collection(tl).items()
                 if owner == f"occurrence:{row['occurrence_id']}/clip:{entry['source']['id']}"}
        copies.append((row, sid, new_sid, shot, mapped_source, owned))
    for row, sid, new_sid, shot, mapped_source, owned in copies:
        shots[new_sid] = shot
        row['shot_id'] = new_sid
        if isinstance(mapping, dict):
            mapping['shots'][new_sid] = mapped_source
            mapping['placements'][row['occurrence_id']]['shot_id'] = new_sid
        collection(tl).update(owned)
        base = tl.bundle.get('base_placements', {}).get(row['occurrence_id'])
        if base is not None:
            base['shot_id'] = new_sid
    for sid in {item[1] for item in copies}:
        del shots[sid]
        if isinstance(mapping, dict):
            mapping['shots'].pop(sid, None)


def owner_for(tl, clip):
    # Use D's exact primitive when its command source is composed by A. This
    # compiler-only continuation also runs before that command producer lands.
    from astrid.sdk.timeline import commands
    if hasattr(commands, 'scoped_clip'):
        return commands.scoped_clip(tl, clip)
    rows = [row for row in tl.bundle.get('placements', []) if str(row.get('shot_id')) == clip.shot_id]
    if len(rows) != 1 or not rows[0].get('occurrence_id'):
        raise MusicBedError(f'{clip.id}: authored music requires occurrence-local closure')
    return f"occurrence:{rows[0]['occurrence_id']}/clip:{clip.id}"


def locate(tl, owner, source):
    """Resolve the existing ScopedId through placements, never through outputs."""
    if owner == f"parent/clip:{source['id']}":
        return None, tl.bundle['parent'].setdefault('clips', [])
    hits = [row for row in tl.bundle.get('placements') or []
            if owner == f"occurrence:{row.get('occurrence_id')}/clip:{source['id']}"]
    if len(hits) != 1:
        raise MusicBedError(f'{owner}: authored music requires unambiguous occurrence-local ownership')
    sid = str(hits[0]['shot_id'])
    if sum(str(row.get('shot_id')) == sid for row in tl.bundle['placements']) != 1:
        raise MusicBedError(f'{owner}: materialize occurrence-local authoring shots before mutation')
    return sid, tl._internal(sid)['clips']


def authored_clip(tl, query):
    """F lookup seam; D's command/entity hook consumes the same source state."""
    from astrid.sdk.timeline.model import Clip
    beds = collection(tl)
    matches = [(owner, entry) for owner, entry in beds.items()
               if owner == query or (entry.get('source') or {}).get('id') == query]
    if len(matches) > 1:
        raise MusicBedError(f'{query}: ambiguous authored sources; use an occurrence ScopedId')
    if matches:
        owner, entry = matches[0]
        sid, _ = locate(tl, owner, entry['source'])
        # An unenrolled clip with the same alias is also a competing entity.
        if query != owner and any(c.id == query and c.shot_id != sid for c in tl.clips()):
            raise MusicBedError(f'{query}: ambiguous authored sources; use an occurrence ScopedId')
        return Clip(tl, sid, entry['source'])
    rows = [row for row in tl.bundle.get('placements') or []
            if query.startswith(f"occurrence:{row.get('occurrence_id')}/clip:")]
    if len(rows) > 1:
        raise MusicBedError(f'{query}: duplicate occurrence identity')
    if rows:
        row = rows[0]
        prefix = f"occurrence:{row['occurrence_id']}/clip:"
        sid = str(row['shot_id'])
        clips = [c for c in tl._internal(sid)['clips'] if prefix + str(c.get('id')) == query]
        if len(clips) != 1:
            raise MusicBedError(f'{query}: missing or duplicate scoped clip')
        clip = Clip(tl, sid, clips[0])
        clip._occurrence_id = row['occurrence_id']
        return clip
    return None


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
    authored = set()
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
        source_key = sid, entry['source']['id']
        if source_key in authored:
            raise MusicBedError(f'{owner}: duplicate authored music ownership')
        authored.add(source_key)
        ids = [c['id'] for c in expected]
        if not ids and any(c.get('id') == entry['source']['id'] for c in clips):
            raise MusicBedError(f'{owner}: authored source ID collides with an unrelated clip')
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
            if ((clip.start < a < clip.end) or (delta < 0 and a <= clip.start < b)) and id(clip.data) not in skip_ids:
                owner = owner_for(tl, clip)
                locate(tl, owner, clip.data)
                from astrid.sdk.timeline.doc import _timeline_stamp
                snapshot = copy.deepcopy(clip.data)
                if _timeline_stamp(snapshot.get('_timeline')):
                    snapshot.pop('_timeline')
                pending.setdefault(owner, (clip.shot_id, snapshot, clip.data, tl._shot_start(clip.shot_id)))
        tl._music_pending = pending
    for owner, entry in beds.items():
        sid, _ = locate(tl, owner, entry['source'])
        source = entry['source']
        old_shot = tl._shot_start(sid)
        old_start = old_shot + float(source.get('at') or 0)
        from astrid.core.timeline.cuts import clip_duration
        end = old_start + clip_duration(source) + sum(p['delta_s'] for p in entry['ripple_basis'])
        excluded = owner in skipped or id(source) in skip_ids
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
    for owner, (sid, source, original_data, origin) in getattr(tl, '_music_pending', {}).items():
        if owner in beds:
            continue
        basis = canonical_basis([dict(at_s=p, delta_s=d) for p, d, skip in records if id(original_data) not in skip])
        source['at'] = round(float(source.get('at') or 0) + origin - tl._shot_start(sid), 6)
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
        original[0].clear()
        original[0].update(source)
        beds[owner]['source'] = original[0]
        notes.extend(report)
    return notes


def source_edit(method):
    """Route existing Clip operations to authored intent before runner registration."""
    @wraps(method)
    def call(clip, *args, **kwargs):
        rows = [row for row in clip._tl.bundle.get('placements', [])
                if str(row.get('shot_id')) == clip.shot_id]
        if len(rows) > 1:
            oid = getattr(clip, '_occurrence_id', None)
            if oid is None:
                raise MusicBedError(f'{clip.id}: ambiguous shared input requires occurrence-local closure')
            local_id = clip.id
            prepare(clip._tl)
            row = next(row for row in clip._tl.bundle['placements'] if row['occurrence_id'] == oid)
            from astrid.sdk.timeline.model import Clip
            sid = str(row['shot_id'])
            bound = Clip(clip._tl, sid, next(c for c in clip._tl._internal(sid)['clips'] if c.get('id') == local_id))
            result = call(bound, *args, **kwargs)
            # Rebind a retained view only after a successful mutation. A refusal
            # leaves the original view attached to the restored source object.
            clip.shot_id, clip.data = bound.shot_id, bound.data
            return clip if result is bound else result
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


@contextmanager
def occurrence_changes(bundle):
    """Keep F carriers coherent inside existing authoring placement helpers.

    The caller owns the edit/history boundary. Only the detached document is
    restored on refusal; no additional command or undo state is introduced.
    """
    from astrid.sdk.timeline.checkout import Checkout
    tl = object.__new__(Checkout)
    tl.bundle = bundle
    from astrid.core.timeline.cuts import bundle_fps
    tl.fps = bundle_fps(bundle) or 30.0
    if not collection(tl):
        yield
        return
    before = copy.deepcopy(bundle)
    validate(tl)
    starts = {owner: tl._shot_start(locate(tl, owner, entry['source'])[0])
              for owner, entry in collection(tl).items()}
    try:
        yield
        beds = collection(tl)
        for owner in list(starts):
            entry = beds[owner]
            rows = [row for row in bundle.get('placements', [])
                    if owner == f"occurrence:{row.get('occurrence_id')}/clip:{entry['source']['id']}"]
            if owner.startswith('occurrence:') and not rows:
                # The shot helper already removed that occurrence's container.
                del beds[owner]
                continue
            sid, _ = locate(tl, owner, entry['source'])
            shift = tl._shot_start(sid) - starts[owner]
            entry['ripple_basis'] = canonical_basis([
                dict(at_s=p['at_s'] + shift, delta_s=p['delta_s']) for p in entry['ripple_basis']])
        project(tl)
        validate(tl)
    except BaseException:
        bundle.clear()
        bundle.update(before)
        raise


def duplicate_occurrence_beds(bundle, source_sid, new_sid, occurrence_id):
    """Consume the existing authoring helper's new occurrence/shot mapping."""
    from astrid.sdk.timeline.checkout import Checkout
    tl = object.__new__(Checkout)
    tl.bundle = bundle
    beds = collection(tl)
    originals = [(owner, entry) for owner, entry in list(beds.items())
                 if locate(tl, owner, entry['source'])[0] == source_sid]
    for owner, entry in originals:
        new_owner = f"occurrence:{occurrence_id}/clip:{entry['source']['id']}"
        if new_owner in beds:
            raise MusicBedError(f'{new_owner}: duplicate music ownership')
        from astrid.sdk.timeline_editing import _remap_authoring_item_refs
        old_items = (bundle['shots'][source_sid].get('base_payload') or {}).get('items', [])
        new_items = (bundle['shots'][new_sid].get('base_payload') or {}).get('items', [])
        item_ids = {old.get('item_id', old.get('id')): new.get('item_id', new.get('id'))
                    for old, new in zip(old_items, new_items)
                    if old.get('item_id', old.get('id')) and new.get('item_id', new.get('id'))}
        cloned = copy.deepcopy(entry)
        cloned['source'] = _remap_authoring_item_refs(cloned['source'], item_ids)
        cloned['owner'] = new_owner
        shift = tl._shot_start(new_sid) - tl._shot_start(source_sid)
        cloned['ripple_basis'] = canonical_basis([
            dict(at_s=p['at_s'] + shift, delta_s=p['delta_s']) for p in cloned['ripple_basis']])
        # The existing helper copied the baseline. Replace this authored bed's
        # baseline source/spans while preserving every unrelated cloned clip.
        clips = tl._internal(new_sid)['clips']
        owned = set(entry['outputs']) | {entry['source']['id']}
        index = min((i for i, c in enumerate(clips) if c.get('id') in owned), default=len(clips))
        clips[:] = [c for c in clips if c.get('id') not in owned]
        clips.insert(index, cloned['source'])
        beds[new_owner] = cloned
