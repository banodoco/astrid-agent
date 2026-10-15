"""Documented structural gestures, executed only by the canonical command runner.

These handlers consume exact authored owners, never UI groups or ambient media.
Compound/tied edits need the caller's explicit choice; no ripple is inferred.
"""

from __future__ import annotations

import copy
from typing import Any

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TIME_RE, TimelineEditError
from astrid.sdk.timeline.doc import _trim_end, _trim_front
from astrid.sdk.timeline.model import Clip


def _literal_source(clip):
    from astrid.sdk.timeline import commands

    sid = commands.scoped_clip(clip._tl, clip)
    if (
        sid in commands.music_carriers(clip._tl)
        or intent.on(clip.data)
        or intent.until(clip.data)
        or intent.cut_of(clip.data)
        or intent.sequence(clip.data)
        or intent.line(clip.data)
        or intent.formulas(clip.data)
        or intent.loop(clip.data)
        or intent.orphan(clip.data)
    ):
        raise TimelineEditError(
            "Structural gesture touches tied/grouped content; choose keep, detach, adjust offset or re-anchor explicitly"
        )
    if any(key in clip.data for key in ("keyframes", "modifiers", "keyframeOrigin")):
        raise TimelineEditError(
            "Animated structural copy/split needs an explicit animation partition contract"
        )
    if clip.data.get("hold") is not None and any(key in clip.data for key in ("from", "to")):
        raise TimelineEditError(
            "Combined hold and source range needs an explicit media split/copy contract"
        )
    clip._tl._require_independent_clip(clip)
    return sid


def duplicate(
    self, *, from_: str, at: Any, track: str | None = None, clip_id: str | None = None
) -> Clip:
    """Copy an exact source within its recorded owner; placement is explicit."""
    tl = self
    from astrid.sdk.timeline import commands

    if not commands.is_scoped_id(from_) or "/clip:" not in from_:
        raise TimelineEditError("timeline.add from requires an exact clip ScopedId")
    source = commands.recorded_owner(
        tl, commands.EditCommand("timeline.add", None, {}, resolved=(from_,)), "clip"
    )
    _literal_source(source)
    start = tl.quantize(
        tl._moment_time(at, source)
        if isinstance(at, str) and not TIME_RE.fullmatch(at.strip())
        else tl.time(at)
    )
    if start < 0 or (source.shot_id is not None and tl._shot_at(start) != source.shot_id):
        raise TimelineEditError(
            "Duplicate within the source owner; cross-owner insertion requires explicit asset/track mapping"
        )
    data = copy.deepcopy(source.data)
    data["id"] = clip_id or tl._new_id(source.id)
    data["at"] = start - tl._shot_start(source.shot_id)
    if track is not None:
        if not any(t.get("id") == track for t in tl._internal(source.shot_id).get("tracks", [])):
            raise TimelineEditError(f"No existing track {track!r} in the source owner")
        data["track"] = track
    # This form is intentionally literal: anchored placement needs its own
    # explicit intent choice rather than detaching an authored source silently.
    if isinstance(at, str):
        if not TIME_RE.fullmatch(at.strip()):
            raise TimelineEditError(
                "Anchored duplicate placement requires an explicit intent choice"
            )
    tl._internal(source.shot_id).setdefault("clips", []).append(data)
    return Clip(tl, source.shot_id, data)


def split(self, at: Any) -> Clip:
    """Split one literal clip, keeping its identity on the left and minting right."""
    clip = self
    _literal_source(clip)
    tl = clip._tl
    point = tl.quantize(
        tl._moment_time(at, clip)
        if isinstance(at, str) and not TIME_RE.fullmatch(at.strip())
        else tl.time(at)
    )
    if not clip.start < point < clip.end:
        raise TimelineEditError("Split point must be strictly inside the selected clip")
    end = clip.end
    offset = tl.quantize(point - clip.start)
    data = copy.deepcopy(clip.data)
    data["id"] = tl._new_id(clip.id)
    _trim_front(data, offset)
    data["at"] = point - tl._shot_start(clip.shot_id)
    _trim_end(clip.data, offset)
    if intent.for_s(clip.data) is not None:
        intent.set_for(clip.data, offset)
        intent.set_for(data, tl.quantize(end - point))
    tl._internal(clip.shot_id)["clips"].append(data)
    return Clip(tl, clip.shot_id, data)
