"""Source-derived interval plan for a managed, single-source music bed.

Only numeric intervals are updated while planning. Clip dictionaries and fades
are emitted once, after every editorial input has been considered.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
from dataclasses import dataclass

from astrid.core.timeline.cuts import clip_duration

EPS = 1e-6


def _bar_len(bars: list[float]) -> float:
    """The bed's bar: the median gap between its downbeats (source seconds), else 2.0 s."""
    pts = sorted(float(x) for x in bars)
    gaps = [b - a for a, b in zip(pts, pts[1:]) if b - a > EPS]
    return statistics.median(gaps) if gaps else 2.0


def _bed_clusters(records: list[tuple[float, float]], bar: float, frame: float) -> list[tuple[float, float]]:
    """The changes of one bed, as (film second, net seconds): neighbours less than a bar apart are one cut at the
    first one's place with their net delta; a net under a frame is no cut."""
    groups: list[list[tuple[float, float]]] = []
    for p, d in sorted(records, key=lambda r: r[0]):
        if groups and p - groups[-1][-1][0] < bar - EPS:
            groups[-1].append((p, d))
        else:
            groups.append([(p, d)])
    out = []
    for group in groups:
        net = sum(d for _, d in group)
        if abs(net) >= frame - EPS:
            out.append((group[0][0], net))
    return out


def canonical_basis(points):
    totals = {}
    for point in points:
        if not isinstance(point, dict) or set(point) != {'at_s', 'delta_s'}:
            raise ValueError('ripple_basis entries require at_s and delta_s')
        at, delta = point['at_s'], point['delta_s']
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (at, delta)):
            raise ValueError('ripple_basis requires finite seconds')
        at = round(at, 6)
        totals.setdefault(at, []).append(delta)
    return [dict(at_s=at, delta_s=net) for at in sorted(totals)
            if (net := round(math.fsum(totals[at]), 6)) != 0]


def move_point(p, a, b, delta):
    if delta < 0 and p >= b - EPS:
        return p + delta
    if delta < 0 and p > a + EPS:
        return a
    if delta > 0 and p > a + EPS:
        return p + delta
    return p


@dataclass
class _Span:
    at: float
    lo: float
    hi: float
    fade_in: float | None = None
    fade_out: float | None = None


def allocate_output_ids(output, occupied, *, established=()):
    """Allocate fresh derived aliases without displacing any authored identity.

    The input is a newly materialized plan. Reserve all its preferred aliases
    first so a fallback cannot steal a later span's established identity.
    """
    occupied = set(occupied)
    if output and output[0]['id'] in occupied:
        raise ValueError('authored source ID collision')
    reserved = occupied | {clip['id'] for clip in output} | set(established)
    for i, clip in enumerate(output[1:], 1):
        preferred = clip['id']
        previous = established[i] if i < len(established) else ''
        suffix = previous.removeprefix(preferred + '-')
        if (previous == preferred or (previous.startswith(preferred + '-')
                                      and suffix.isdecimal() and str(int(suffix)) == suffix and int(suffix) >= 2)) \
                and previous not in occupied:
            # The carrier records an already allocated alias. Keep it even if
            # the unrelated clip that originally occupied the base is removed.
            clip['id'] = previous
        elif preferred in occupied:
            n = 2
            while f'{preferred}-{n}' in reserved:
                n += 1
            clip['id'] = f'{preferred}-{n}'
            reserved.add(clip['id'])
        occupied.add(clip['id'])
    return output


def materialize(source, basis, owner, *, start=0.0, fps=30.0, bars=(), beats=(), fixed_seams=None, seams_out=None):

    speed = float(source.get('speed') or 1)
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError('music source speed must be positive finite seconds')
    at = start + float(source.get('at') or 0)
    lo = float(source.get('from') or 0)
    hi = lo + clip_duration(source) * speed
    spans = [_Span(at, lo, hi)] if hi > lo + EPS else []
    changed = False
    notes = []
    changes = (fixed_seams if fixed_seams is not None else
               _bed_clusters([(p['at_s'], p['delta_s']) for p in basis], _bar_len(bars), 1 / fps))
    for point, delta in changes:
        index = next((i for i, s in enumerate(spans)
                      if (s.at < point or (delta < 0 and abs(s.at - point) < EPS))
                      and point < s.at + (s.hi - s.lo) / speed - EPS), None)
        if index is None:
            continue
        span = spans[index]
        seam, seam_source = point, span.lo + (point - span.at) * speed
        kind = None
        for label, points in (() if fixed_seams is not None else (('bar', bars), ('beat', beats))):
            candidates = [p for p in points if span.lo - EPS <= p <= seam_source + EPS]
            if candidates:
                candidate = max(candidates)
                if span.at + (candidate - span.lo) / speed >= span.at + .5 - EPS:
                    seam, seam_source, kind = span.at + (candidate - span.lo) / speed, candidate, label
                break
        if seams_out is not None:
            seams_out.append((seam, delta))
        overlap = min(.25, 2 * (seam - span.at))
        half = overlap / 2
        second_at = seam - half
        if delta < 0:
            resume = seam_source - delta * speed - half * speed
        else:
            needed = seam_source - delta * speed - half * speed
            resume = max(span.lo, needed)
            second_at += (resume - needed) / speed
        replacement = [_Span(span.at, span.lo, min(span.hi, seam_source + half * speed), span.fade_in, overlap)]
        if resume < span.hi - EPS:
            replacement.append(_Span(second_at, resume, span.hi, overlap, span.fade_out))
        spans[index:index + 1] = [s for s in replacement if s.hi > s.lo + EPS]
        changed = True
        silence = max(0, second_at - seam - half) if delta > 0 else 0
        notes.append(f'music {source["id"]}: cut {"on the " + kind if kind else "at the change"} at {seam:.2f} s, '
                     f'{"repeats" if delta > 0 else "skips"} {abs(delta):.2f} s, crossfade {overlap:.2f} s; '
                     + (f'{silence:.2f} s of silence (not enough music before the seam to repeat)' if silence > .005
                        else 'its ending stays on the film\'s end'))
    output = []
    for i, span in enumerate(spans):
        clip = copy.deepcopy(source)
        clip['at'] = round(span.at - start, 6)
        if changed:
            clip['from'] = round(span.lo, 6)
            clip.pop('duration', None)
            if source.get('hold') is None and source.get('to') is not None:
                clip.pop('hold', None)
                clip['to'] = round(span.hi, 6)
            else:
                clip.pop('to', None)
                clip['hold'] = round((span.hi - span.lo) / speed, 6)
            params = clip.setdefault('params', {})
            if span.fade_in is not None:
                params['fadeIn'] = round(span.fade_in, 6)
            if span.fade_out is not None:
                params['fadeOut'] = round(span.fade_out, 6)
        if i:
            # The established one-cut identity remains compatible. For a larger
            # plan, scope and the complete resulting interval/order determine IDs.
            if len(spans) == 2:
                clip['id'] = f'{source["id"]}:b'
            else:
                identity = [owner, i, round(span.at, 6), round(span.lo, 6), round(span.hi, 6)]
                digest = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()[:20]
                clip['id'] = f'{source["id"]}:b-{digest}'
            clip.setdefault('app', {})['split_from'] = source['id']
        output.append(clip)
    return output, notes, changed
