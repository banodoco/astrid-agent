"""Content-derived cut windows and stable identities for every timeline consumer.

The cache holds detached derivations, never a checkout's mutable clip objects.
On a hit rows are rebound to the caller's occurrences. No cut record is stored.
Timing's next-opening boundaries are a separate projection from picture coverage:
using those boundaries for a view would erase picture gaps and overlaps.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from typing import Any

from .astrid_ns import read_app
from .cuts import (
    bundle_fps,
    clip_duration,
    find_cut,
    occurrences_from_bundle,
    occurrences_from_snapshot,
    picture_cuts,
)

_CACHE: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()


class CutIndex:
    def __init__(self, occurrences, *, fps=30.0):
        self.occurrences = list(occurrences)
        self.fps = fps
        # Include metadata as well as timing: names, orphan flags, sequence/cut
        # tags and layer changes all invalidate the derived projection.
        content = [{k: v for k, v in o.items() if k not in {'shot', 'registry'}} for o in self.occurrences]
        self.digest = hashlib.sha256(json.dumps([fps, content], sort_keys=True, default=str).encode()).hexdigest()
        if self.digest not in _CACHE:
            rows = picture_cuts(self.occurrences, fps=fps)
            represented = {(r['occurrence_id'], r.get('cut_id')) for r in rows if r.get('cut_id')}
            for o in self.occurrences:
                named = {}
                for span in o['spans']:
                    cid = read_app(span['clip']).get('cut')
                    if cid:
                        named.setdefault(str(cid), []).append(span)
                for cid, members in named.items():
                    if (o['occurrence_id'], cid) in represented:
                        continue
                    # An authored cut survives a missing picture through its
                    # members, including members flagged orphan by narration edits.
                    lo, hi = min(s['start'] for s in members), max(s['end'] for s in members)
                    rows.append(dict(start=lo, end=hi, duration=hi-lo, occurrence_id=o['occurrence_id'],
                                     shot_id=o['shot_id'], shot=o.get('name', o['shot_id']), cut_id=cid,
                                     clip=None, clip_id=None, track=None, type=None,
                                     layers=[s for s in members if not s['audio']], sequence=None,
                                     deliberate_hold=False, orphan=True))
            rows.sort(key=lambda r: (r['start'], r['end'], r.get('cut_id') or ''))
            for n, row in enumerate(rows, 1):
                row['index'] = n
                o = next(o for o in self.occurrences if o['occurrence_id'] == row['occurrence_id'])
                row['audio'] = [s for s in o['spans'] if s['audio'] and s['end'] > row['start'] and s['start'] < row['end']]
            _CACHE[self.digest] = copy.deepcopy(rows)
            if len(_CACHE) > 32:
                _CACHE.popitem(last=False)
        _CACHE.move_to_end(self.digest)
        self.rows = copy.deepcopy(_CACHE[self.digest])
        by_occurrence = {o['occurrence_id']: {s['id']: s for s in o['spans']} for o in self.occurrences}
        for row in self.rows:
            spans = by_occurrence[row['occurrence_id']]
            if row['clip_id'] is not None:
                row['clip'] = spans[row['clip_id']]['clip']
            for kind in ('layers', 'audio'):
                row[kind] = [spans[s['id']] for s in row.get(kind, [])]

    def resolve(self, selector):
        text = str(selector).strip().lower()
        for row in self.rows:
            if row.get('cut_id') == text:
                return row
        try:
            return find_cut(self.rows, selector)
        except ValueError:
            # SDK clip prefixes remain accepted alongside exact snapshot ids.
            for row in self.rows:
                ids = [row.get('clip_id') or ''] + list((row.get('sequence') or {}).get('clip_ids', []))
                if text and any(cid.startswith(text) for cid in ids):
                    return row
            raise

    def groups(self, tl, *, include_orphans=False):
        """Authored membership, distinct from the intersections in ``rows``.

        Structural commands act on members; views act on intersections. Sound
        without a cut tag can never enter a structural group.
        """
        from astrid.sdk import timeline_intent as intent
        groups = {}
        for clip in tl.clips():
            cid = intent.cut_of(clip.data)
            if cid:
                groups.setdefault(cid, []).append(clip)
        out = []
        for cid, clips in groups.items():
            orphan = all(intent.orphan(c.data) for c in clips)
            if orphan and not include_orphans:
                continue
            rows = [r for r in self.rows if r.get('cut_id') == cid]
            pictures = {id(r['clip']) for r in rows if r['clip'] is not None}
            beds = sorted((c for c in clips if id(c.data) in pictures), key=lambda c: (c.start, c.id))
            picture = beds[0] if beds else None
            out.append(dict(id=cid, clips=clips, picture=picture,
                            start=picture.start if picture else min(c.start for c in clips),
                            end=max((r['end'] for r in rows), default=max(c.end for c in clips)), orphan=orphan))
        return sorted(out, key=lambda g: (g['start'], g['id']))

    def spans(self):
        """Actual named picture/placeholder coverage, including spanning sequences."""
        result = {}
        for row in self.rows:
            cid = row.get('cut_id')
            if cid:
                lo, hi = result.get(cid, (row['start'], row['end']))
                result[cid] = (min(lo, row['start']), max(hi, row['end']))
        return result

    def intersections(self, selector):
        row = self.resolve(selector)
        return dict(start=row['start'], end=row['end'],
                    visual=[s['id'] for s in row['layers']], audio=[s['id'] for s in row['audio']])

    def doomed_cuts(self, tl, sid, start, end, skip_ids=()):
        """Stable cuts whose complete picture coverage is inside a ripple window."""
        from astrid.sdk import timeline_intent as intent
        tol = .5 / self.fps
        dying = {}
        for row in self.rows:
            cid = row.get('cut_id')
            if row['shot_id'] != sid or not cid or row['clip'] is None:
                continue
            ids = (row.get('sequence') or {}).get('clip_ids') or [row['clip_id']]
            spans = next(o['spans'] for o in self.occurrences if o['occurrence_id'] == row['occurrence_id'])
            members = [s['clip'] for s in spans if s['id'] in ids]
            dies = all(id(c) not in skip_ids and not intent.orphan(c)
                       and float(c.get('at', 0)) >= start - tol
                       and float(c.get('at', 0)) + clip_duration(c) <= end + tol
                       for c in members)
            dying[cid] = dying.get(cid, True) and dies
        return {cid for cid, dies in dying.items() if dies}

    def timing_spans(self, tl, skip=()):
        skip = set(skip) | set(getattr(tl, '_orphan_cuts', ()) or ())
        groups = [g for g in self.groups(tl) if g['id'] not in skip]
        return {g['id']: (g['start'], groups[i+1]['start'] if i+1 < len(groups) else None)
                for i, g in enumerate(groups)}


def cut_index(occurrences, *, fps=30.0):
    return CutIndex(occurrences, fps=fps)


def bundle_cut_index(bundle):
    return CutIndex(occurrences_from_bundle(bundle), fps=bundle_fps(bundle))


def snapshot_cut_index(snapshot, *, fps=30.0):
    return CutIndex(occurrences_from_snapshot(snapshot), fps=fps)


def resolve_cut(rows, selector):
    """Resolve a projection from this index, retaining legacy ordinal inputs."""
    for row in rows:
        if row.get('cut_id') == str(selector).strip().lower():
            return row
    return find_cut(rows, selector)
