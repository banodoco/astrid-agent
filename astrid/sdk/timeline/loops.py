"""Compile authored loop phase using the timeline's existing moment/beat clock."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict

from astrid.core.timeline import moments
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.model import _MomentContext


class PhaseReference(TypedDict):
    ref: str


class FramePeriod(TypedDict):
    frames: int


class BeatPeriod(TypedDict):
    beats: int
    bed: str


class LoopSpec(TypedDict):
    phase: PhaseReference
    period: FramePeriod | BeatPeriod
    beforePhase: NotRequired[Literal['continue']]


class _BedContext(_MomentContext):
    def __init__(self, tl, clip, bed):
        super().__init__(tl, clip)
        self.bed = bed

    def beats(self, kind):
        # Reuse exactly the placed-beat mapping in _MomentContext, with the
        # authored bed plus its existing split_from descendants as the input.
        return super().beats(kind, bed=self.bed)


def _positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TimelineEditError(f'loop {label} must be a positive integer')
    return value


def _frame(seconds, fps):
    return round(moments.floor_frame(seconds, fps) * fps)


def resolve_loop(tl, clip, spec: LoopSpec) -> dict:
    """Produce optional params.loop; never write onset, key origin or media time.

    A beat period requires phase on an actual placed beat. Only complete groups
    of the requested beat count are usable, including groups before phase.
    Missing adjacent beats are an error rather than an extrapolated tempo.
    """
    if clip.data.get('clipType') != 'am-sprite':
        raise TimelineEditError('authored loop requires the am-sprite consumer')
    if not isinstance(spec, Mapping) or set(spec) - {'phase', 'period', 'beforePhase'}:
        raise TimelineEditError('loop must contain phase, period and optional beforePhase')
    phase, period = spec.get('phase'), spec.get('period')
    if not isinstance(phase, Mapping) or set(phase) != {'ref'} or not isinstance(phase['ref'], str):
        raise TimelineEditError('loop phase requires a moment ref')
    if not isinstance(period, Mapping):
        raise TimelineEditError('loop period requires frames or beats with bed')
    if spec.get('beforePhase', 'continue') != 'continue':
        raise TimelineEditError('loop beforePhase must be continue')
    beat_period = set(period) == {'beats', 'bed'}
    frame_period = set(period) == {'frames'}
    if not beat_period and not frame_period:
        raise TimelineEditError('loop period requires frames or beats with bed')
    ctx = _MomentContext(tl, clip)
    if beat_period:
        _positive_int(period['beats'], 'beats')
        bed = period['bed']
        if not isinstance(bed, str) or not bed:
            raise TimelineEditError('loop beat period requires a bed id')
        roots = [c for c in tl.clips(audio=True) if c.id == bed]
        if len(roots) != 1:
            raise TimelineEditError(f'loop bed {bed!r} must identify exactly one music clip')
        ctx = _BedContext(tl, clip, bed)
    else:
        _positive_int(period['frames'], 'frames')
    try:
        phase_seconds = moments.resolve(phase['ref'], ctx)
    except moments.MomentError as exc:
        raise TimelineEditError(f'loop phase {phase["ref"]}: {exc}') from None
    if not math.isfinite(phase_seconds):
        raise TimelineEditError('loop phase must resolve to a finite time')
    out = {'phaseFrame': _frame(phase_seconds, tl.fps)}
    if frame_period:
        return {**out, 'periodFrames': period['frames']}

    # Exact duplicates can arise in overlapping segments; distinct source beats
    # that collapse to one render frame cannot define a positive loop interval.
    beats = sorted(set(ctx.beats('beat')))
    frames = [_frame(b, tl.fps) for b in beats]
    if any(b <= a for a, b in zip(frames, frames[1:])):
        raise TimelineEditError('loop adjacent beats must occupy distinct frames')
    if out['phaseFrame'] not in frames:
        raise TimelineEditError('loop beat phase must resolve to an actual placed beat of its bed')
    anchor, n = frames.index(out['phaseFrame']), period['beats']
    boundaries = [frame for i, frame in enumerate(frames) if (i - anchor) % n == 0]
    start = math.floor(clip.start * tl.fps + 0.5)
    end = start + max(1, math.floor(clip.duration * tl.fps + 0.5))
    if len(boundaries) < 2 or start < boundaries[0] or end > boundaries[-1]:
        raise TimelineEditError('loop bed lacks adjacent placed beats covering the clip interval')
    return {**out, 'beatFrames': frames, 'periodBeats': n}


def resolve_loops(tl) -> list[str]:
    changes = []
    for clip in tl.clips():
        spec = intent.loop(clip.data)
        if spec is None:
            continue
        value = resolve_loop(tl, clip, spec)
        params = clip.data.setdefault('params', {})
        if params.get('loop') != value:
            params['loop'] = value
            changes.append(f'{clip.id}.params.loop resolved')
    return changes
