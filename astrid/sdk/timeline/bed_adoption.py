"""Detached BED-ADOPT producer; the command runner owns commit/retry/undo.

No operation is registered here. D consumes adopt_music_bed's candidate inside
one existing command transaction, recording the storage move as authored intent.
"""
from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Mapping

from astrid.core.timeline import astrid_ns
from astrid.core.timeline.cuts import clip_duration
from astrid.sdk.timeline.music_beds import (
    MusicBedError,
    adopted,
    collection,
    locate,
    materialize,
    prepare,
    sources,
)


def _number(value, label, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise MusicBedError(f'{label} must be {"positive " if positive else ""}finite seconds')
    return float(value)


def _placement(tl, owner, source):
    from astrid.sdk.timeline.model import Clip
    sid, _ = locate(tl, owner, source)
    # Alignment runs after the ordinary compiler resolves timing expressions.
    # Elsewhere read its cached result, just as the legacy carrier validator
    # does: resolving a changed anchor before dematerialization would mistake a
    # legitimate pending regeneration for a supplied output edit.
    return Clip(tl, sid, source).start


def projections(tl, arrangement, origin):
    source = arrangement['source']
    rate = _number(source.get('speed', 1), 'source speed', positive=True)
    gain = source.get('volume', (source.get('params') or {}).get('volume', 1))
    gain = _number(gain, 'source gain')
    offset = _number(source.get('from', 0), 'source offset') - rate * (
        _placement(tl, arrangement['owner'], source) - origin)
    if not isinstance(source.get('asset'), str) or not source['asset']:
        raise MusicBedError('adopted arrangement needs an ordinary source asset')
    return dict(asset=source['asset'], source_offset_s=round(offset, 6), rate=rate, gain=gain)


def refresh_projections(tl):
    """Only call after an authored source edit; loading checks aliases instead."""
    bed = adopted(tl)
    if bed is None:
        return
    reference = next((a for a in bed['arrangements'] if a['owner'] == bed['owner']), None)
    if reference is None:
        raise MusicBedError('bed.owner must retain its stable reference source')
    origin = _placement(tl, reference['owner'], reference['source'])
    for arrangement in bed['arrangements']:
        arrangement.update(projections(tl, arrangement, origin))


def switch_windows(tl, bed=None):
    """Derived results only. Missing moments are ORPHANs, never nearest matches.

    The current TimeRef vocabulary is the existing time()/Moment string grammar.
    D owns command bindings and explicit re-home/replay through that same grammar.
    """
    from astrid.core.timeline.audio_curves import validate as validate_curve
    bed = adopted(tl) if bed is None else bed
    windows, ids = [], set()
    for switch in bed['switches']:
        if not isinstance(switch, dict) or set(switch) != {'id', 'on', 'duration', 'curve'}:
            raise MusicBedError('switch requires id, on, duration and curve')
        if not isinstance(switch['id'], str) or not switch['id'] or switch['id'] in ids:
            raise MusicBedError('duplicate or missing bed switch identity')
        ids.add(switch['id'])
        duration = _number(switch['duration'], 'switch duration', positive=True)
        if not isinstance(switch['on'], str) or not switch['on'].strip():
            raise MusicBedError('switch on requires an existing TimeRef string')
        try:
            validate_curve(switch['curve'])
            center = _number(tl.time(switch['on']), 'switch moment')
        except (ValueError, LookupError) as exc:
            raise MusicBedError(f'ORPHAN switch {switch["id"]}: {exc}; re-home on an exact surviving TimeRef') from None
        lo, hi = center - duration / 2, center + duration / 2
        if lo < 0:
            raise MusicBedError(f'{switch["id"]}: negative switch window')
        if windows and lo < windows[-1][1] - 1e-6:
            raise MusicBedError('overlapping switch windows imply three active arrangements')
        windows.append((lo, hi))
    return windows


def validate_adopted(tl):
    from astrid.core.timeline.music_bed import canonical_basis
    bed = adopted(tl)
    if bed is None:
        return
    if set(bed) != {'owner', 'arrangements', 'switches', 'ripple_basis', 'outputs'}:
        raise MusicBedError('malformed adopted bed record')
    if not isinstance(bed['arrangements'], list) or not bed['arrangements']:
        raise MusicBedError('bed requires authored arrangements')
    if not isinstance(bed['switches'], list) or len(bed['switches']) != len(bed['arrangements']) - 1:
        raise MusicBedError('bed requires one ordered switch between adjacent arrangements')
    if not isinstance(bed['ripple_basis'], list) or canonical_basis(bed['ripple_basis']) != bed['ripple_basis']:
        raise MusicBedError('bed ripple_basis must be canonical')
    if not isinstance(bed['outputs'], dict):
        raise MusicBedError('bed outputs must be keyed by arrangement ID')
    ids, owners = set(), set()
    for a in bed['arrangements']:
        if not isinstance(a, dict) or set(a) != {'id', 'owner', 'source', 'asset', 'source_offset_s', 'rate', 'gain'}:
            raise MusicBedError('malformed adopted arrangement')
        if not isinstance(a['id'], str) or not a['id'] or a['id'] in ids:
            raise MusicBedError('duplicate or missing arrangement identity')
        if not isinstance(a['owner'], str) or a['owner'] in owners:
            raise MusicBedError('duplicate or missing arrangement owner')
        if not isinstance(a['source'], dict) or not isinstance(a['source'].get('id'), str) or not a['source']['id']:
            raise MusicBedError('arrangement requires its complete ordinary source')
        ids.add(a['id'])
        owners.add(a['owner'])
        if a['owner'] in collection(tl):
            raise MusicBedError(f'{a["owner"]}: duplicate authored music authority')
        if a['id'] not in bed['outputs'] or not isinstance(bed['outputs'][a['id']], list):
            raise MusicBedError('missing arrangement output ownership (an empty list is valid)')
    if ids != set(bed['outputs']) or bed['owner'] not in owners:
        raise MusicBedError('bed outputs/reference disagree with arrangements')
    reference = next(a for a in bed['arrangements'] if a['owner'] == bed['owner'])
    origin = _placement(tl, reference['owner'], reference['source'])
    from astrid.sdk.timeline.music_beds import _materialize_entry, _shared_seams
    shared_seams = _shared_seams(tl)
    spans = []
    for a in bed['arrangements']:
        expected = projections(tl, a, origin)
        if any(type(a[k]) is bool or a[k] != value for k, value in expected.items()):
            raise MusicBedError(f'{a["id"]}: arrangement projections conflict with source; edit the authored source')
        source = a['source']
        lo = _number(source.get('from', 0), 'source in-point')
        if source.get('hold') is not None:
            raw_length = _number(source['hold'], 'source hold')
        elif source.get('to') is not None:
            raw_length = _number(source['to'], 'source out-point') - lo
        else:
            raw_length = _number(source.get('duration', 0), 'source duration')
        if raw_length < 0:
            raise MusicBedError('negative source bounds or interval')
        length = _number(clip_duration(source), 'source duration')
        if lo < 0 or length < 0:
            raise MusicBedError('negative source bounds or interval')
        sid, _ = locate(tl, a['owner'], source)
        asset = tl._registry(sid).get(source['asset'])
        if not isinstance(asset, Mapping):
            raise MusicBedError(f'{a["id"]}: missing source media asset')
        if asset.get('duration') is not None and lo + length * expected['rate'] > float(asset['duration']) + 1e-6:
            raise MusicBedError('source bounds are outside media coverage')
        output, _, _ = _materialize_entry(tl, a['owner'], sources(tl)[a['owner']], shared_seams=shared_seams)
        intervals = sorted((tl._shot_start(sid) + float(c.get('at') or 0),
                            tl._shot_start(sid) + float(c.get('at') or 0) + clip_duration(c)) for c in output)
        merged = []
        for start, end in intervals:
            if end <= start:
                continue
            if merged and start <= merged[-1][1] + 1e-6:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        spans.append(merged)
    windows = switch_windows(tl, bed)
    for index, (lo, hi) in enumerate(windows):
        for intervals in spans[index:index + 2]:
            if not any(start - 1e-6 <= lo and hi <= end + 1e-6 for start, end in intervals):
                raise MusicBedError('switch window is outside adjacent source coverage')
    # Even with disjoint declared switches, three ordinary authored spans would
    # remain audible: adoption never silently trims them to make a bed fit.
    events = sorted([(lo, 1) for intervals in spans for lo, hi in intervals] +
                    [(hi, -1) for intervals in spans for lo, hi in intervals])
    active = 0
    for _, change in events:
        active += change
        if active > 2:
            raise MusicBedError('source spans imply three simultaneously active arrangements')
    return shared_seams


def _clean_source(source):
    from astrid.sdk.timeline.doc import _timeline_stamp
    out = copy.deepcopy(source)
    if _timeline_stamp(out.get('_timeline')):
        out.pop('_timeline')
    # These are compiler bookkeeping, not opaque authored metadata.
    for app in [out.get('app'), (out.get('app') or {}).get('astrid')]:
        if isinstance(app, dict):
            for key in ('split_from', 'music_beds', 'bed', 'outputs', 'ripple_basis'):
                app.pop(key, None)
    return out


def adopt_music_bed(bundle, selected, *, switches=(), alignment=None, common_basis=None):
    """Return a compiled detached candidate; never mutate the supplied bundle.

    selected is an ordered list of existing ScopedIds (common origin = first).
    alignment is owner -> {confidence: 1.0, source_offset_s: number}. Only an
    explicitly exact alignment is admitted here; the align action is evidence,
    not an automatic retime. No missing source is inferred from output pieces.
    common_basis optionally supplies an explicit canonical candidate; it must
    preserve every known output mapping and is never constructed by summing bases.
    """
    from astrid.sdk.timeline import commands
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.compiler import compile_timing
    from astrid.sdk.timeline.music_beds import _materialize_entry, _shared_seams, validate
    if not isinstance(selected, (list, tuple)) or not selected or any(not isinstance(s, str) for s in selected):
        raise MusicBedError('select ordered exact source ScopedIds')
    if len(set(selected)) != len(selected):
        raise MusicBedError('duplicate selected source identity')
    from astrid.core.timeline.compatibility import (
        ADOPTED_BED_CAPABILITY,
        require_writable_candidate,
    )
    require_writable_candidate(bundle, capabilities={ADOPTED_BED_CAPABILITY})
    tl = Checkout(copy.deepcopy(bundle))
    prepare(tl)
    validate(tl)
    # Resolve existing timing expressions on the detached candidate before
    # checking alignment. This is the current compiler, not an adoption clock.
    tl = Checkout(compile_timing(tl.document()).bundle)
    existing = adopted(tl)
    if existing is not None:
        if selected == [a['owner'] for a in existing['arrangements']] and list(switches) == existing['switches']:
            if common_basis is not None and common_basis != existing['ripple_basis']:
                raise MusicBedError('idempotent adoption cannot replace the shared basis')
            return compile_timing(tl.document()).bundle
        raise MusicBedError('an adopted bed already exists; edit its authored arrangements')
    carriers = sources(tl)
    originals, arrangements, bases = {}, [], []
    for owner in selected:
        if not commands.is_scoped_id(owner) or '/clip:' not in owner:
            raise MusicBedError(f'{owner}: select an exact existing clip ScopedId')
        if owner in carriers:
            entry = carriers[owner]
            source = _clean_source(entry['source'])
            basis = copy.deepcopy(entry['ripple_basis'])
            expected, _, _ = materialize(source, basis, owner,
                                         start=tl._shot_start(locate(tl, owner, source)[0]), fps=tl.fps)
        else:
            source_id = owner.split('/clip:', 1)[1]
            sid, clips = locate(tl, owner, dict(id=source_id))
            found = [c for c in clips if c.get('id') == source_id]
            if len(found) != 1:
                raise MusicBedError(f'{owner}: missing or ambiguous intact plain source; cannot recover from generated outputs')
            from astrid.sdk import timeline_intent as intent
            if intent._app(found[0]).get('split_from') or any(intent._app(c).get('split_from') == source_id for c in clips):
                raise MusicBedError(f'{owner}: segmented output has no verified intact source')
            source = _clean_source(found[0])
            if not tl._is_music(sid, source):
                raise MusicBedError(f'{owner}: selected source is not music')
            # Resolve declarations on a detached ordinary source; retain timing
            # form in the authoritative source rather than replacing it by seconds.
            start = _placement(tl, owner, source)
            resolved = copy.deepcopy(source)
            resolved['at'] = start - tl._shot_start(sid)
            expected, _, _ = materialize(resolved, [], owner, start=tl._shot_start(sid), fps=tl.fps)
            basis = []
        if any(type(v) is bool for v in (source.get('at'), source.get('from'), source.get('speed'))):
            raise MusicBedError('boolean source timing is invalid')
        originals[owner] = expected
        bases.append(basis)
        arrangement_id = 'arrangement-' + hashlib.sha256(owner.encode()).hexdigest()[:20]
        arrangements.append(dict(id=arrangement_id, owner=owner, source=source))
    # One carrier transfers byte-for-byte. For several, a basis is acceptable
    # only if it preserves every known mapping. Never sum/concatenate copies.
    candidates = []
    if common_basis is not None:
        from astrid.core.timeline.music_bed import canonical_basis
        if not isinstance(common_basis, list) or canonical_basis(common_basis) != common_basis:
            raise MusicBedError('explicit common basis must be canonical')
        if len(bases) == 1 and common_basis != bases[0]:
            raise MusicBedError('single-carrier adoption preserves its basis exactly')
        candidates.append(copy.deepcopy(common_basis))
    for basis in (bases if common_basis is None else []):
        if basis not in candidates:
            candidates.append(basis)
    bed = dict(owner=selected[0], arrangements=arrangements, switches=copy.deepcopy(list(switches)),
               ripple_basis=[], outputs={a['id']: [c['id'] for c in originals[a['owner']]] for a in arrangements})
    ns = astrid_ns.film_ns(tl.bundle.setdefault('parent', {}).setdefault('config', {}))
    ns['v'] = 2
    ns['bed'] = bed
    for owner in selected:
        collection(tl).pop(owner, None)
    refresh_projections(tl)
    if not isinstance(alignment, Mapping) or set(alignment) != set(selected):
        raise MusicBedError('provide exact alignment evidence for every selected source')
    for a in arrangements:
        evidence = alignment[a['owner']]
        if not isinstance(evidence, Mapping) or type(evidence.get('confidence')) not in (int, float) or evidence['confidence'] != 1:
            raise MusicBedError(f'{a["owner"]}: ambiguous or low-confidence alignment; provide an exact confirmed alignment')
        offset = _number(evidence.get('source_offset_s'), 'alignment source_offset_s')
        if abs(offset - a['source_offset_s']) > 1e-6:
            raise MusicBedError(f'{a["owner"]}: alignment disagrees with source placement; adoption never retimes sources')
    compatible = False
    for basis in candidates:
        bed['ripple_basis'] = copy.deepcopy(basis)
        shared_seams = _shared_seams(tl)
        if all(_materialize_entry(tl, owner, sources(tl)[owner], shared_seams=shared_seams)[0] == originals[owner] for owner in selected):
            compatible = True
            break
    if not compatible:
        raise MusicBedError('incompatible ripple mappings: no verified common basis preserves every source; supply an exact common_basis or align authored sources explicitly before adopting')
    validate(tl)
    result = compile_timing(tl.document()).bundle
    validate(Checkout(copy.deepcopy(result)))
    return result


def reconcile_arrangement_aliases(before, after):
    """Translate unambiguous public alias edits on a detached candidate.

    D's reconcile caller records source edits through its existing command path.
    Contradictory source/alias edits refuse rather than selecting a winner.
    """
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.music_beds import project, validate
    tl = Checkout(copy.deepcopy(after))
    prior_tl = Checkout(copy.deepcopy(before))
    validate(prior_tl)
    old = adopted(prior_tl)
    bed = adopted(tl)
    if old is None or bed is None:
        raise MusicBedError('alias reconciliation requires the same adopted bed')
    if old['outputs'] != bed['outputs']:
        raise MusicBedError('generated output ownership is read-only')
    for owner, entry in sources(prior_tl).items():
        if owner not in sources(tl):
            raise MusicBedError('arrangement owner changes require an authored bed command')
        _, old_clips = locate(prior_tl, owner, entry['source'])
        _, new_clips = locate(tl, owner, sources(tl)[owner]['source'])
        if ([c for c in old_clips if c.get('id') in entry['outputs']] !=
                [c for c in new_clips if c.get('id') in entry['outputs']]):
            raise MusicBedError('generated music spans are read-only; edit the authored source or arrangement alias')
    old_by_id = {a['id']: a for a in old['arrangements']}
    if set(old_by_id) != {a['id'] for a in bed['arrangements']} or old['owner'] != bed['owner']:
        raise MusicBedError('arrangement identity changes require an authored bed command')
    reference = next(a for a in bed['arrangements'] if a['owner'] == bed['owner'])
    origin = _placement(tl, reference['owner'], reference['source'])
    for a in bed['arrangements']:
        prior = old_by_id[a['id']]
        expected = projections(tl, a, origin)
        aliases = [k for k in expected if a[k] != prior[k]]
        if a['owner'] != prior['owner']:
            raise MusicBedError('arrangement owner changes require an authored bed command')
        if a['source'] != prior['source'] and any(a[k] != expected[k] for k in aliases):
            raise MusicBedError('contradictory arrangement source/alias edits')
        if a['source'] == prior['source']:
            source = a['source']
            for key in aliases:
                if key == 'asset':
                    source['asset'] = a[key]
                elif key == 'rate':
                    source['speed'] = _number(a[key], 'source rate', positive=True)
                elif key == 'gain':
                    source['volume'] = _number(a[key], 'source gain')
            if 'source_offset_s' in aliases or 'rate' in aliases:
                new_from = _number(a['source_offset_s'], 'source offset') + float(source.get('speed', 1)) * (
                    _placement(tl, a['owner'], source) - origin)
                delta = new_from - float(source.get('from', 0))
                source['from'] = new_from
                if source.get('to') is not None:
                    source['to'] += delta
    refresh_projections(tl)
    project(tl)
    validate(tl)
    return tl.document()
