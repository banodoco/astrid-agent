"""Chapters as stored shots, one flat timeline for editing.

The edit engine works on ONE shot whose clip times are film seconds. This module converts at the two
storage boundaries:

* ``flatten(stored)``: a stored film (one shot per chapter, plus film-level clips, registry and track order on the
  parent) becomes the flat working shot. The stored form is kept under ``structure_base``.
* ``split(flat)``: the flat shot becomes the stored form. Each chapter is a real shot (a split film's chapters are
  its shot placements, anchored to cuts in the flat form; a one-shot film's come from its labels, once, at restructure); music and unsupported clips that cross a chapter wall (by more than 1 ms) are film-level clips.

``renderer_input`` / ``assert_equivalent`` project a stored form exactly as the renderer does and prove two forms
hand the renderer the same thing, so a restructure that would change a frame or a sample refuses to publish.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from astrid.core.timeline.cuts import bundle_fps, clip_duration
from astrid.core.timeline.parent_registry import effective_parent_registry
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import AUDIO_KINDS, TimelineEditError, _num, _r
from astrid.sdk.timeline.doc import _filter_words, _row_start, _set_length, _set_row_start

STRUCTURE_KEY = "structure_base"
WALL_TOLERANCE = 0.001  # a clip crossing a wall by no more than 1 ms stays in its chapter
_PIECE_MARKER = "timeline_restructure_piece"
_ORDER_MARKER = "timeline_restructure_order"


_OVER_MARKER = "timeline_restructure_over"
_OVER_ROW_MARKER = "timeline_restructure_over_row"


def _clip_kind(clip: Mapping[str, Any]) -> str:
    return str(clip.get("clipType") or clip.get("clip_type") or clip.get("type") or clip.get("kind") or "media").lower()


def _cutter_for(clip: Mapping[str, Any]):
    """Dispatch hook for faithfully cuttable visual types; None keeps the clip on an over row."""
    kind = _clip_kind(clip)
    if kind in {"media", "image", "video", "still", "text"}:
        return _cut_clip
    if kind == "am-sprite":
        return _cut_sprite
    return None


def _js_round(value: float) -> int:
    """Match JavaScript Math.round, which sends half values toward +infinity."""
    return math.floor(value + 0.5)


def _cut_safety_reason(clip: Mapping[str, Any], cut_starts: Iterable[float], fps: float,
                       chapters: Iterable[Chapter] | None = None) -> str | None:
    """Explain why cutting changes the renderer's frame interval or source phase."""

    for field_name in ("entrance", "exit", "continuous", "animation", "animations", "effects", "transition",
                       "keyframes", "keyframeOrigin", "fade_in", "fade_out", "fadeIn", "fadeOut"):
        value = clip.get(field_name)
        if value not in (None, [], {}, ""):
            return f"{field_name} timing restarts at a chapter cut"
    params = clip.get("params")
    if isinstance(params, Mapping):
        for field_name in ("animation", "animations", "effects", "fade_in", "fade_out", "fadeIn", "fadeOut",
                           "fadeInCurve", "fadeOutCurve", "keyframeOrigin"):
            if params.get(field_name) not in (None, [], {}, ""):
                return f"params.{field_name} timing restarts at a chapter cut"
        if _clip_kind(clip) != "am-sprite" and params.get("keyframes") not in (None, [], {}, ""):
            return "params.keyframes timing restarts at a chapter cut"
    source_start = _num(clip.get("at"))
    original_placement = _js_round(source_start * fps)
    original_duration = max(1, _js_round(clip_duration(clip) * fps))
    original_end = original_placement + original_duration
    cuts = sorted(set(cut_starts))
    starts = [source_start, *cuts]

    # Remotion rounds each Sequence start and duration separately. A chapter
    # piece first gets a chapter-relative, six-decimal `at`, then the actual
    # shot-composition projection adds the chapter start and recomputes its
    # visible length. Use those normalized coordinates and lengths here.
    cursor = original_placement
    cutter = _cutter_for(clip)
    chapter_rows = sorted(chapters or [], key=lambda row: row.start_ms)
    projected_piece_starts: list[int | None] = []
    projected_pieces: list[dict[str, Any] | None] = []
    for index, start in enumerate(starts):
        piece_end = starts[index + 1] if index + 1 < len(starts) else source_start + clip_duration(clip)
        chapter = next((row for row in chapter_rows
                        if row.start_ms / 1000.0 <= start + 1e-9
                        and start < row.end_ms / 1000.0 - 1e-9), None)
        if chapter is None:
            chapter_start = _js_round(start * 1000)
            chapter_end = math.ceil(piece_end * 1000)
            if chapter_end <= chapter_start:
                chapter_end = chapter_start + 1
            chapter = Chapter("", chapter_start, chapter_end)
        piece = (cutter(clip, start, piece_end, chapter, fps=fps)
                 if cutter is _cut_sprite else cutter(clip, start, piece_end, chapter))
        from astrid.core.timeline.shot_composition_projection import _project_clip

        projected = _project_clip(
            piece,
            occurrence={"occurrence_id": "cut-check", "shot_id": "cut-check",
                        "output_identity": {}, "stable_deep_link": {}, "source_offset": 0},
            revision={"shot_id": "cut-check", "revision_id": "cut-check-revision",
                      "internal_timeline_revision": {"revision_id": "cut-check-internal"}},
            occurrence_start=chapter.start_ms / 1000.0,
            occurrence_end=chapter.end_ms / 1000.0,
            occurrence_speed=1.0,
            track_definitions=[],
            path="cut-safety",
        )
        if projected is None:
            projected_piece_starts.append(None)
            projected_pieces.append(None)
            continue
        piece_start_frame = _js_round(_num(projected.get("at")) * fps)
        projected_piece_starts.append(piece_start_frame)
        projected_pieces.append(projected)
        piece_duration_frames = max(1, _js_round(clip_duration(projected) * fps))
        piece_end_frame = piece_start_frame + piece_duration_frames
        if piece_start_frame != cursor or piece_end_frame > original_end:
            return f"renderer frame interval differs after cut at {start:g} s"
        cursor = piece_end_frame
    if any(piece is None for piece in projected_pieces):
        duration_only_sprite = (_clip_kind(clip) == "am-sprite" and clip.get("duration") is not None
                                and clip.get("hold") is None and clip.get("to") is None)
        if duration_only_sprite and all(piece is None for piece in projected_pieces):
            # Duration-only sprites are not admitted by the shot projection.
            # Keeping their cut pieces in the shot preserves that empty render
            # result and lets flatten rejoin the authored source unchanged.
            return None
        missing_index = next(i for i, piece in enumerate(projected_pieces) if piece is None)
        boundary = starts[missing_index + 1] if missing_index + 1 < len(starts) else starts[missing_index]
        return f"renderer frame interval differs after cut at {boundary:g} s"
    assert all(frame is not None for frame in projected_piece_starts)
    if cuts and cursor != original_end:
        return f"renderer frame interval differs after cut at {cuts[-1]:g} s"

    if _clip_kind(clip) in {"media", "video"} and cuts:
        source_from = _num(clip.get("from"))
        speed = _num(clip.get("speed"), 1.0) or 1.0
        original_trim = _js_round(max(0.0, source_from) * fps)
        for piece_index, piece_start in enumerate(projected_piece_starts):
            assert piece_start is not None and projected_pieces[piece_index] is not None
            piece_from = _num(projected_pieces[piece_index].get("from"))
            piece_trim = _js_round(max(0.0, piece_from) * fps)
            piece_placement = piece_start
            if piece_index and abs(piece_from * fps - round(piece_from * fps)) > 1e-7:
                return f"fractional renderer source offset at piece {starts[piece_index]:g} s"
            expected_trim = original_trim + (piece_placement - original_placement) * speed
            if abs(piece_trim - expected_trim) > 1e-7:
                return f"continuous renderer source phase differs at piece {starts[piece_index]:g} s"
    if _clip_kind(clip) == "am-sprite" and cuts:
        params = clip.get("params") if isinstance(clip.get("params"), Mapping) else {}
        prior_clock = params.get("sourceClock") if isinstance(params.get("sourceClock"), Mapping) else {}
        prior_offset = _num(prior_clock.get("offsetFrames"))
        prior_speed = _num(prior_clock.get("speed"), 1.0) or 1.0
        speed = _num(clip.get("speed"), 1.0) or 1.0
        for cut_index, start in enumerate(cuts, 1):
            piece = cutter(clip, start, source_start + clip_duration(clip),
                           next((row for row in chapter_rows
                                 if row.start_ms / 1000.0 <= start + 1e-9
                                 and start < row.end_ms / 1000.0 - 1e-9),
                                Chapter("", 0, _js_round((source_start + clip_duration(clip)) * 1000))),
                           fps=fps)
            piece_params = piece.get("params") if isinstance(piece.get("params"), Mapping) else {}
            clock = piece_params.get("sourceClock") if isinstance(piece_params.get("sourceClock"), Mapping) else {}
            clock_offset = _num(clock.get("offsetFrames"))
            clock_speed = _num(clock.get("speed"), 1.0) or 1.0
            actual_offset = projected_piece_starts[cut_index] - original_placement
            expected_offset = (prior_offset + actual_offset) / speed if prior_clock else actual_offset
            expected_speed = prior_speed * speed
            if abs(clock_offset - expected_offset) > 1e-7 or abs(clock_speed - expected_speed) > 1e-7:
                return f"sprite renderer clock differs after cut at {start:g} s"
    return None


def _piece_shape(clip: Mapping[str, Any]) -> str:
    if clip.get("hold") is not None:
        return "hold_window" if clip.get("from") is not None and clip.get("to") is not None else "hold"
    if clip.get("from") is not None or clip.get("to") is not None:
        return "window"
    return "duration" if clip.get("duration") is not None else "hold"


def _cut_clip(clip: Mapping[str, Any], start: float, end: float, chapter: Chapter,
              *, piece_id: str | None = None) -> dict[str, Any]:
    """Make one chapter-relative interval of a media clip, advancing its source by elapsed * speed."""
    value = copy.deepcopy(dict(clip))
    speed = _num(clip.get("speed"), 1.0) or 1.0
    elapsed = max(0.0, start - _num(clip.get("at")))
    if piece_id is not None:
        value["id"] = piece_id
    value["at"] = _r(max(0.0, start - chapter.start_ms / 1000.0))
    if _clip_kind(clip) in {"media", "video"}:
        value["from"] = _r(_num(clip.get("from")) + elapsed * speed)
    length = max(0.0, end - start)
    shape = _piece_shape(clip)
    if shape in {"hold", "hold_window", "window"}:
        _set_length(value, length)
    else:
        value["duration"] = _r(length * speed)
    _filter_words(value, elapsed, elapsed + length, shift=-elapsed)
    return value


def _cut_sprite(clip: Mapping[str, Any], start: float, end: float, chapter: Chapter,
                *, piece_id: str | None = None, fps: float = 30.0) -> dict[str, Any]:
    """Cut an am-sprite while keeping its original clip-local clock exactly."""
    value = _cut_clip(clip, start, end, chapter, piece_id=piece_id)
    params = copy.deepcopy(dict(clip.get("params") or {})) if isinstance(clip.get("params"), Mapping) else {}
    speed = _num(clip.get("speed"), 1.0) or 1.0
    offset = max(0, _js_round(start * fps) - _js_round(_num(clip.get("at")) * fps))
    prior_clock = params.get("sourceClock") if isinstance(params.get("sourceClock"), Mapping) else {}
    prior_offset = _num(prior_clock.get("offsetFrames"))
    prior_speed = _num(prior_clock.get("speed"), 1.0) or 1.0
    # Sprite keyframes are stored on each piece's local clock. Preserve the pose
    # already active at this cut as the piece's initial pose; later keys remain
    # pending until their original activation step.
    frame_offset = _num(params.get("frameOffset"))
    step = max(1, int(_num(params.get("stepFrames"), 2)))
    clock_offset = offset if not prior_clock else (prior_offset + offset) / speed
    clock_speed = prior_speed * speed
    prior_origin = math.floor(prior_offset * prior_speed) if prior_clock else 0
    origin = math.floor(clock_offset * clock_speed)
    source_phase = origin - prior_origin
    source_cut = math.floor((origin + frame_offset) / step) * step - frame_offset - prior_origin
    pose = {key: params[key] for key in ("x", "y", "flipX") if key in params}
    has_keyframes = isinstance(params.get("keyframes"), list)
    future = []
    keys = params.get("keyframes", []) if has_keyframes else []
    for key in sorted((key for key in keys if isinstance(key, Mapping)),
                      key=lambda key: _num(key.get("frame"))):
        if _num(key.get("frame"), -1) <= source_cut:
            for field in ("x", "y", "flipX"):
                if field in key:
                    pose[field] = key[field]
    for key in keys:
        if not isinstance(key, Mapping):
            continue
        if _num(key.get("frame"), -1) <= source_cut:
            continue
        else:
            shifted = copy.deepcopy(dict(key))
            # A pending key may precede local frame zero while waiting for the
            # next held step. Zero has the same activation and remains admissible.
            shifted["frame"] = max(0, int(_num(key.get("frame")) - source_phase))
            future.append(shifted)
    if offset or prior_clock:
        params.update(pose)
        if has_keyframes:
            params["keyframes"] = future
    params["sourceClock"] = {"offsetFrames": _r(clock_offset),
                              "speed": clock_speed}
    value["speed"] = 1.0
    # _cut_clip computed length using the original speed; pieces now run at 1x.
    if "hold" in value:
        value["hold"] = _r((end - start))
    if "from" in value and "to" in value:
        value["to"] = _r(_num(value["from"]) + (end - start))
    if _piece_shape(clip) == "duration":
        value["duration"] = _r(end - start)
    value["params"] = params
    return value


# ------------------------------------------------------------------ reading the stored form

def _rows(bundle: Mapping[str, Any], *, base: bool = False) -> list[dict[str, Any]]:
    rows: Any = None
    if base:
        mapped = (bundle.get("source_mapping") or {}).get("placements")
        if isinstance(mapped, Mapping) and mapped:
            rows = list(mapped.values())
        elif isinstance(mapped, list) and mapped:
            rows = mapped
        elif isinstance(bundle.get("base_placements"), Mapping) and bundle["base_placements"]:
            rows = list(bundle["base_placements"].values())
    if rows is None:
        rows = bundle.get("placements") or []
    return sorted((r for r in rows if isinstance(r, dict)), key=_row_start)


def _shot_name(shot: Mapping[str, Any], *, base: bool = False) -> str:
    payload = (shot.get("base_payload") if base and shot.get("base_payload") else shot.get("payload")) or {}
    return str(payload.get("name") or shot.get("shot_id") or "")


def _chapter_runs(bundle: Mapping[str, Any]) -> list[dict[str, str]]:
    runs: list[dict[str, str]] = []
    for row in intent.chapters(bundle):
        if not runs or runs[-1]["name"] != str(row.get("name")):
            runs.append({"name": str(row.get("name")), "from": str(row.get("from"))})
    return runs


def is_split(bundle: Mapping[str, Any]) -> bool:
    """True for a stored film that places more than one shot (its chapters are those shots), or that holds
    film-level clips next to one shot named like its single chapter label."""
    if not isinstance(bundle, Mapping) or STRUCTURE_KEY in bundle:
        return False
    rows = _rows(bundle)
    shots = bundle.get("shots") or {}
    if not rows or any(str(r.get("shot_id")) not in shots for r in rows):
        return False
    if len(rows) >= 2:  # a chapter film: distinct shot names, a cut in every shot, fits the flat form (else it is edited as it is)
        names = [_shot_name(shots[str(r["shot_id"])]) for r in rows]
        if len(set(names)) != len(names):
            return False
        try:
            for r in rows:
                _check_shot(r, shots[str(r["shot_id"])])
        except TimelineEditError:
            return False
        return all(any(intent.cut_of(c) for c in (shots[str(r["shot_id"])].get("internal_timeline") or {}).get("clips") or [])
                   for r in rows)
    parent = bundle.get("parent") or {}
    has_clips = bool(parent.get("clips") or (parent.get("config") or {}).get("clips"))
    runs = _chapter_runs(bundle)
    return has_clips and [r["name"] for r in runs] == [_shot_name(shots[str(rows[0]["shot_id"])])]


def shot_chapters(bundle: Mapping[str, Any]) -> list[dict[str, str]] | None:
    """The chapters of a film that places more than one shot: ``[{name, from: cutId}]``, one per shot placement
    (name = the shot's name, ``from`` = the first cut starting inside its span). None when the film is not such a
    film, so the caller falls back to the label list."""
    if not isinstance(bundle, Mapping) or STRUCTURE_KEY in bundle or len(bundle.get("placements") or []) < 2:
        return None
    try:
        flat = flatten(dict(bundle))
    except TimelineEditError:
        return None
    return [dict(r) for r in intent.film_chapter_labels(flat)] if STRUCTURE_KEY in flat else None


def _anchor_chapters(out: dict[str, Any], rows: list[dict[str, Any]], shots: Mapping[str, Any]) -> None:
    """Write the flat form's working chapter anchors from the stored shots: each shot's name at the first cut that
    starts inside its span, so the walls follow later edits. split() drops them again."""
    from astrid.sdk.timeline.checkout import Checkout

    groups = Checkout(copy.deepcopy(out))._cut_groups()
    starts = [(int(round(g["start"] * 1000)), g["id"]) for g in groups]
    table: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        lo = _row_start(row)
        hi = _row_start(rows[index + 1]) if index + 1 < len(rows) else lo + int(_num(row.get("duration_ms")))
        name = _shot_name(shots[str(row["shot_id"])])
        cut = next((cid for ms, cid in starts if lo - 1.5 <= ms < hi - 1.5), None)
        if cut is None:
            raise TimelineEditError(f"shot {name!r} has no cut starting inside it, so it cannot be edited as chapter {name!r}")
        table.append({"name": name, "from": cut})
    intent.set_chapters(out, table, _anchors=True)


def _check_shot(row: Mapping[str, Any], shot: Mapping[str, Any]) -> None:
    """Refuse what a flat shot cannot carry (as ``one_shot`` does)."""
    duration = float(row.get("duration_ms") or 0)
    placement = row.get("placement") or {}
    if not isinstance(placement, Mapping) or set(placement) - {"start_ms", "track"} or placement.get("track", "picture") != "picture":
        raise TimelineEditError("cannot flatten placement geometry")
    if row.get("speed", {"numerator": 1, "denominator": 1}) not in (1, {"numerator": 1, "denominator": 1}):
        raise TimelineEditError("cannot flatten placement speed")
    offset = row.get("source_offset") or {}
    if not isinstance(offset, Mapping) or set(offset) - {"start", "end"} or offset.get("start", 0) != 0 or offset.get("end", 0) not in (0, duration):
        raise TimelineEditError("cannot flatten source offsets")
    if row.get("gain", 1) != 1 or row.get("muted", False) is not False or row.get("track", "picture") != "picture":
        raise TimelineEditError("cannot flatten placement gain, mute, or track")
    allowed = {"shot_id", "occurrence_id", "placement", "duration_ms", "source_offset", "speed", "track", "gain", "muted"}
    if any(v not in (None, {}, [], False) for k, v in row.items() if k not in allowed):
        raise TimelineEditError("cannot flatten placement metadata or transforms")
    internal = shot.get("internal_timeline") or {}
    if any(v not in (None, {}, []) for k, v in internal.items() if k not in {"clips", "tracks", "registry"}):
        raise TimelineEditError("cannot flatten timeline-wide fields")
    if any(v not in (None, {}, []) for k, v in (internal.get("registry") or {}).items() if k != "assets"):
        raise TimelineEditError("cannot flatten registry metadata")


def _view(bundle: Mapping[str, Any], *, base: bool) -> dict[str, Any]:
    """One side (current or pinned base) of a stored film as parts to flatten."""
    parent = bundle.get("base_parent_payload") if base and isinstance(bundle.get("base_parent_payload"), Mapping) else bundle.get("parent")
    rows = [r for r in _rows(bundle, base=base) if str(r.get("shot_id")) in (bundle.get("shots") or {})]
    shots = {}
    for row in rows:
        sid = str(row["shot_id"])
        shot = bundle["shots"][sid]
        internal = (shot.get("base_internal_timeline") if base and shot.get("base_internal_timeline") else shot.get("internal_timeline")) or {}
        payload = (shot.get("base_payload") if base and shot.get("base_payload") else shot.get("payload")) or {}
        shots[sid] = {"internal_timeline": internal, "payload": payload}
        _check_shot(row, {"internal_timeline": internal})
    return {"parent": parent or {}, "rows": rows, "shots": shots}


_IDENTITY = ("media_id", "content_sha256", "file")


def _identity(entry: Mapping[str, Any]) -> dict[str, str]:
    out = {}
    for name in _IDENTITY:
        value = entry.get(name)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if name == "content_sha256":
                value = value.lower().removeprefix("sha256:")
            out[name] = value
    return out


def same_asset(a: Any, b: Any) -> bool:
    """Whether two registry entries name the same media (the Runtime's rule, runtime_protocol/asset_identity.py).

    Identity fields are ``media_id``, ``content_sha256`` (optional ``sha256:`` prefix dropped, lower-cased) and
    ``file``. Same media iff the entries share at least one identity field and every identity field both carry is
    equal; a field only one side carries never counts against them, and decoration (urls, thumbnails, etags,
    handles, names) never counts at all. With no shared identity field the entries are compared by full equality."""
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return a == b
    ia, ib = _identity(a), _identity(b)
    shared = ia.keys() & ib.keys()
    return a == b if not shared else all(ia[k] == ib[k] for k in shared)


def _union_asset(held: Any, value: Any, error: str) -> Any:
    """Copies of one asset key that name the same media merge: the held copy's values win for fields both carry,
    then fields only the new copy has are added. Different media raises ``error``."""
    if held == value:
        return copy.deepcopy(held)
    if not same_asset(held, value):
        raise TimelineEditError(error)
    return {**copy.deepcopy(dict(held)), **{k: copy.deepcopy(v) for k, v in value.items() if k not in held}}


def _piece_marker(clip: Mapping[str, Any]) -> Mapping[str, Any] | None:
    app = clip.get("app")
    astrid = app.get("astrid") if isinstance(app, Mapping) else None
    marker = astrid.get(_PIECE_MARKER) if isinstance(astrid, Mapping) else None
    return marker if isinstance(marker, Mapping) else None


def _rejoin_pieces(clips: list[dict[str, Any]], chapter_start: float, fps: float,
                   chapters: Iterable[Chapter] | None = None) -> list[dict[str, Any]]:
    """Restore complete, unchanged cutter groups; leave incomplete or edited pieces alone."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for clip in clips:
        marker = _piece_marker(clip)
        if marker and marker.get("id") is not None:
            groups.setdefault(str(marker["id"]), []).append(clip)
    consumed: set[int] = set()
    replacements: dict[int, dict[str, Any]] = {}
    for source_id, pieces in groups.items():
        first_marker = _piece_marker(pieces[0]) or {}
        count = first_marker.get("count")
        if not isinstance(count, int) or count < 2 or len(pieces) != count:
            continue
        pieces.sort(key=lambda item: (_piece_marker(item) or {}).get("index", -1))
        markers = [_piece_marker(piece) or {} for piece in pieces]
        if [marker.get("index") for marker in markers] != list(range(count)) \
                or any(marker.get("count") != count or marker.get("shape") != first_marker.get("shape")
                       or marker.get("source_from_present") != first_marker.get("source_from_present")
                       or marker.get("source_speed_present") != first_marker.get("source_speed_present")
                       or marker.get("source_speed") != first_marker.get("source_speed") for marker in markers):
            continue
        starts = [chapter_start + _num(piece.get("at")) for piece in pieces]
        lengths = [clip_duration(piece) for piece in pieces]
        if any(abs(starts[i] + lengths[i] - starts[i + 1]) > 1e-6 for i in range(count - 1)):
            continue
        original = copy.deepcopy(pieces[0])
        original["id"] = source_id
        original["at"] = _r(starts[0])
        if first_marker.get("source_from_present") is False:
            original.pop("from", None)
        if first_marker.get("source_speed_present"):
            original["speed"] = copy.deepcopy(first_marker["source_speed"])
        else:
            original.pop("speed", None)
        params = original.get("params")
        if isinstance(params, dict):
            params.pop("sourceClock", None)
        shape = str(first_marker.get("shape"))
        total = sum(lengths)
        speed = _num(original.get("speed"), 1.0) or 1.0
        if shape in {"hold", "hold_window", "window"}:
            _set_length(original, total)
        else:
            original["duration"] = _r(total * speed)
        if shape in {"window", "hold_window"} and _clip_kind(original) != "am-sprite":
            if pieces[-1].get("to") is not None:
                original["to"] = copy.deepcopy(pieces[-1]["to"])
        timed_words = []
        for piece, start in zip(pieces, starts):
            shift = start - starts[0]
            for row in intent.words(piece):
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    timed_words.append([_r(_num(row[0]) + shift), _r(_num(row[1]) + shift), *copy.deepcopy(list(row[2:]))])
                elif isinstance(row, Mapping) and "start" in row and "end" in row:
                    timed_words.append({**copy.deepcopy(dict(row)), "start": _r(_num(row["start"]) + shift),
                                        "end": _r(_num(row["end"]) + shift)})
        if timed_words:
            intent.map_timed(original, lambda _: timed_words)
        app = original.get("app")
        astrid = app.get("astrid") if isinstance(app, dict) else None
        if isinstance(astrid, dict):
            astrid.pop(_PIECE_MARKER, None)
            if not astrid:
                app.pop("astrid", None)
            if not app:
                original.pop("app", None)
        cutter = _cutter_for(original)
        valid = cutter is not None
        chapter_rows = sorted(chapters or [], key=lambda row: row.start_ms)
        piece_chapters = []
        for piece, start, length in zip(pieces, starts, lengths):
            chapter = next((row for row in chapter_rows
                            if row.start_ms / 1000.0 <= start + 1e-9
                            and start < row.end_ms / 1000.0 - 1e-9), None)
            if chapter is None:
                piece_chapter_start = start - _num(piece.get("at"))
                chapter = Chapter("", _js_round(piece_chapter_start * 1000),
                                  _js_round((piece_chapter_start + length) * 1000))
            piece_chapters.append(chapter)
        if valid and _cut_safety_reason(original, starts[1:], fps, piece_chapters):
            continue
        expected_pieces = []
        for piece, start, length in zip(pieces, starts, lengths):
            if not valid:
                break
            chapter_start = start - _num(piece.get("at"))
            chapter = Chapter("", int(round(chapter_start * 1000)),
                              int(round((chapter_start + length) * 1000)))
            expected = cutter(original, start, start + length, chapter,
                              piece_id=str(piece.get("id")), fps=fps) if cutter is _cut_sprite \
                else cutter(original, start, start + length, chapter, piece_id=str(piece.get("id")))
            expected_pieces.append(expected)
        for actual, expected in zip(pieces, expected_pieces):
            actual_clean = copy.deepcopy(actual)
            expected_clean = copy.deepcopy(expected)
            for candidate in (actual_clean, expected_clean):
                app = candidate.get("app")
                astrid = app.get("astrid") if isinstance(app, dict) else None
                if isinstance(astrid, dict):
                    astrid.pop(_PIECE_MARKER, None)
                    if not astrid:
                        app.pop("astrid", None)
                    if not app:
                        candidate.pop("app", None)
            if actual_clean != expected_clean:
                valid = False
                break
        if not valid:
            continue
        positions = {id(clip): index for index, clip in enumerate(clips)}
        first_index = min(positions[id(piece)] for piece in pieces)
        replacements[first_index] = original
        consumed.update(id(piece) for piece in pieces)
    result = []
    for index, clip in enumerate(clips):
        if index in replacements:
            result.append(replacements[index])
        if id(clip) not in consumed:
            result.append(clip)
    return result


def _flat_side(view: Mapping[str, Any]) -> dict[str, Any]:
    parent = copy.deepcopy(dict(view["parent"]))
    config = parent.setdefault("config", {})
    fps = bundle_fps({"parent": parent}) or 30.0
    rows = view["rows"]
    pos = 0
    for row in rows:
        if _row_start(row) != pos:
            raise TimelineEditError("chapter shots must follow each other from 0 without gaps or overlaps to be flattened")
        pos += int(_num(row.get("duration_ms")))
    lists = [x for x in (parent.get("clips"), config.get("clips")) if x]
    if len(lists) == 2 and lists[0] != lists[1]:
        raise TimelineEditError("the parent holds two different clip lists (clips and config.clips)")
    clips = [copy.deepcopy(c) for c in (lists[0] if lists else [])]
    for clip in clips:
        app = clip.get("app")
        astrid = app.get("astrid") if isinstance(app, Mapping) else None
        marker = astrid.pop(_OVER_MARKER, None) if isinstance(astrid, dict) else None
        if isinstance(marker, Mapping):
            clip["track"] = marker.get("track", clip.get("track"))
        if isinstance(astrid, dict) and not astrid:
            app.pop("astrid")
        if isinstance(app, dict) and not app:
            clip.pop("app")
    assets: dict[str, Any] = {}
    tracks: dict[str, Any] = {}

    def add_asset(key: str, value: Any) -> None:
        assets[key] = _union_asset(assets[key], value, f"flatten has conflicting asset key {key!r}") if key in assets \
            else copy.deepcopy(value)

    def add_track(track: Mapping[str, Any], order: list[Mapping[str, Any]], index: int) -> None:
        """Track definitions are copied into the film and every chapter shot; tools may annotate one
        copy (e.g. an App flag under ``app``). Copies merge when they differ only by non-conflicting
        ``app`` keys; on any other difference the film's definition wins (as in the renderer), a shot copy is ignored."""
        key = str(track.get("id"))
        if key not in tracks:
            preceding = next((str(item.get("id")) for item in reversed(order[:index])
                              if str(item.get("id")) in tracks), None) if track in order else None
            following = next((str(item.get("id")) for item in order[index + 1:]
                              if str(item.get("id")) in tracks), None)
            value = copy.deepcopy(dict(track))
            ordered = list(tracks.items())
            if following is None:
                tracks[key] = value
            elif preceding is not None:
                at = next(i + 1 for i, (track_id, _) in enumerate(ordered) if track_id == preceding)
                ordered.insert(at, (key, value))
                tracks.clear()
                tracks.update(ordered)
            else:
                at = next(i for i, (track_id, _) in enumerate(ordered) if track_id == following)
                ordered.insert(at, (key, value))
                tracks.clear()
                tracks.update(ordered)
            return
        held = tracks[key]
        if held == track:
            return
        base_a = {k: v for k, v in held.items() if k != "app"}
        base_b = {k: v for k, v in track.items() if k != "app"}
        app_a = held.get("app") or {}
        app_b = track.get("app") or {}
        if base_a != base_b or not isinstance(app_a, Mapping) or not isinstance(app_b, Mapping) \
                or any(name in app_a and app_a[name] != value for name, value in app_b.items()):
            return  # the film's copy (added first) is the one held: the film's definition wins, as in the renderer
        merged = {**copy.deepcopy(dict(app_a)), **copy.deepcopy(dict(app_b))}
        tracks[key] = {**held, "app": merged}

    for key, value in (effective_parent_registry(parent).get("assets") or {}).items():
        add_asset(key, value)
    film_tracks = config.get("tracks") or []
    for index, track in enumerate(film_tracks):
        add_track(track, film_tracks, index)
    ids = {str(c.get("id")) for c in clips}
    for row in rows:
        sid = str(row["shot_id"])
        internal = view["shots"][sid]["internal_timeline"]
        start = _row_start(row) / 1000.0
        for key, value in (internal.get("registry", {}).get("assets") or {}).items():
            add_asset(key, value)
        shot_tracks = internal.get("tracks") or []
        for index, track in enumerate(shot_tracks):
            add_track(track, shot_tracks, index)
        for clip in internal.get("clips") or []:
            value = copy.deepcopy(clip)
            if str(value.get("id")) in ids:
                raise TimelineEditError(f"flatten has duplicate clip id {value.get('id')!r}")
            ids.add(str(value.get("id")))
            value["at"] = _r(_num(value.get("at")) + start)
            clips.append(value)
    chapter_windows = [Chapter(_shot_name(view["shots"][str(row["shot_id"])]), _row_start(row),
                               _row_start(row) + _num(row.get("duration_ms"))) for row in rows]
    clips = _rejoin_pieces(clips, 0.0, fps, chapters=chapter_windows)
    clips.sort(key=lambda clip: (clip.get("app", {}).get("astrid", {}).get(_ORDER_MARKER, float("inf")),
                                 _num(clip.get("at"))))
    for clip in clips:
        app = clip.get("app")
        astrid = app.get("astrid") if isinstance(app, dict) else None
        if isinstance(astrid, dict):
            astrid.pop(_ORDER_MARKER, None)
            if not astrid:
                app.pop("astrid", None)
            if not app:
                clip.pop("app", None)
    # the parent keeps everything but what moved into the flat shot
    parent["clips"] = []
    config.pop("clips", None)
    config.pop("tracks", None)
    for holder in (parent, config):
        registry = holder.get("registry")
        if isinstance(registry, Mapping):
            holder["registry"] = {k: v for k, v in registry.items() if k != "assets"}
    first = rows[0]
    flat_row = copy.deepcopy(first)
    flat_row["duration_ms"] = pos
    internal0 = copy.deepcopy(view["shots"][str(first["shot_id"])]["internal_timeline"])
    internal0.update(clips=clips, tracks=list(tracks.values()), registry={"assets": assets})
    return {"parent": parent, "row": flat_row, "internal": internal0,
            "payload": copy.deepcopy(view["shots"][str(first["shot_id"])]["payload"]), "shot_id": str(first["shot_id"])}


def flatten(stored: dict[str, Any]) -> dict[str, Any]:
    """The flat working form of a stored chapter film (the same object when it is not one)."""
    if not is_split(stored):
        return stored
    cur = _flat_side(_view(stored, base=False))
    base = cur
    if _rows(stored, base=True):
        try:
            base = _flat_side(_view(stored, base=True))
        except TimelineEditError:
            # A fresh split can add chapter placements to a pinned flat source mapping.
            # Its original one-shot row overlaps those new rows and is not a flattenable base.
            pass
    out = {k: copy.deepcopy(v) for k, v in stored.items()}
    wid = cur["shot_id"]
    out["parent"] = cur["parent"]
    if isinstance(stored.get("base_parent_payload"), Mapping):
        out["base_parent_payload"] = base["parent"]
    out["placements"] = [cur["row"]]
    occ = str(cur["row"].get("occurrence_id"))
    boc = str(base["row"].get("occurrence_id"))
    out["base_placements"] = {boc: copy.deepcopy(base["row"])}
    mapping = copy.deepcopy(stored.get("source_mapping") or {})
    mapping["placements"] = {boc: copy.deepcopy(base["row"])}
    mapping["shots"] = {k: v for k, v in (mapping.get("shots") or {}).items() if k == base["shot_id"] or k == wid}
    out["source_mapping"] = mapping
    shot = {"shot_id": wid, "payload": cur["payload"], "internal_timeline": cur["internal"],
            "base_payload": base["payload"], "base_internal_timeline": base["internal"]}
    if base["shot_id"] != wid:
        shot["shot_id"] = wid
        mapping["shots"][wid] = mapping["shots"].get(base["shot_id"]) or mapping["shots"].get(wid) or {}
    out["shots"] = {wid: shot}
    out[STRUCTURE_KEY] = copy.deepcopy(dict(stored))
    del occ
    rows = _view(stored, base=False)["rows"]
    if len(rows) >= 2:
        _anchor_chapters(out, rows, stored["shots"])
    return out


# ------------------------------------------------------------------ the plan: walls and who stores what

@dataclass
class Chapter:
    name: str
    start_ms: int
    end_ms: int
    clips: list[dict[str, Any]] = field(default_factory=list)  # chapter-relative `at`

    @property
    def length_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass
class Plan:
    chapters: list[Chapter]
    film_clips: list[dict[str, Any]]
    reasons: dict[str, str]  # film-level clip id -> why
    tracks: list[dict[str, Any]]
    assets: dict[str, Any]
    end_ms: int
    over_clips: list[tuple[str, str, str, str]] = field(default_factory=list)


def _walls(tl: Any, runs: list[dict[str, str]], end_ms: int) -> list[int]:
    starts = {g["id"]: g["start"] for g in tl._cut_groups()}
    walls = [0]
    for run in runs[1:]:
        if run["from"] not in starts:
            raise TimelineEditError(f"chapter {run['name']!r} starts at cut {run['from']!r}, which is not in the film")
        # Cut positions can arrive a few microseconds below their frame-aligned
        # value (for example 60.899998). Truncating those values moves the wall
        # one millisecond early and can strand the preceding frame as film-level.
        walls.append(int((starts[run["from"]] * 1000) + 0.5))  # nearest millisecond
    if any(b <= a for a, b in zip(walls, walls[1:])) or walls[-1] >= end_ms:
        raise TimelineEditError("chapter walls must rise strictly inside the film; check the chapter labels: "
                                + ", ".join(f"{r['name']}@{w / 1000:g}s" for r, w in zip(runs, walls)))
    return walls


def _snap_to_shots(walls: list[int], runs: list[dict[str, str]], stored: Mapping[str, Any] | None) -> list[int]:
    """A wall within 1.5 ms of where the film's existing shot of that name is placed lands exactly on that placement
    (cut starts carry millisecond rounding that the stored placement does not), so an unedited film re-splits to
    the same shots; an edited film's walls keep following their cuts."""
    shots = (stored or {}).get("shots") or {}
    starts = {_shot_name(shots[str(r["shot_id"])]): _row_start(r) for r in _rows(stored or {}) if str(r.get("shot_id")) in shots}
    return [int(starts[r["name"]]) if r["name"] in starts and abs(starts[r["name"]] - w) <= 1.5 else w for r, w in zip(runs, walls)]


def plan(flat: Mapping[str, Any]) -> Plan | None:
    """Who stores what for a flat film, or None when it is not a chapter film (one chapter or none)."""
    from astrid.sdk.timeline.checkout import Checkout

    rows = flat.get("placements") or []
    runs = _chapter_runs(flat)
    if len(rows) != 1 or len(runs) < 2:
        return None
    tl = Checkout(copy.deepcopy({k: v for k, v in flat.items() if k != STRUCTURE_KEY}))
    sid = str(rows[0]["shot_id"])
    end_ms = int(_num(rows[0].get("duration_ms")))
    start0 = _row_start(rows[0])
    if start0:
        raise TimelineEditError("the film's shot must start at 0 to be split into chapters")
    walls = _walls(tl, runs, end_ms)
    walls = _snap_to_shots(walls, runs, flat.get(STRUCTURE_KEY))
    occurrences: dict[str, list[tuple[int, str]]] = {}
    for run, wall in zip(runs, walls):
        occurrences.setdefault(run["name"], []).append((wall, run["from"]))
    for name, starts in occurrences.items():
        if len(starts) > 1:
            locations = ", ".join(f"{wall / 1000:.3f} s at {cut}" for wall, cut in starts)
            count = "twice" if len(starts) == 2 else f"{len(starts)} times"
            raise TimelineEditError(f"chapter {json.dumps(name, ensure_ascii=False)} appears {count} "
                                    f"({locations}); rename or remove one label")
    chapters = [Chapter(r["name"], w, (walls[i + 1] if i + 1 < len(walls) else end_ms)) for i, (r, w) in enumerate(zip(runs, walls))]
    end_s = end_ms / 1000.0
    internal = tl.bundle["shots"][sid]["internal_timeline"]
    film: list[dict[str, Any]] = []
    reasons: dict[str, str] = {}
    over_clips: list[tuple[str, str, str, str]] = []
    fps = bundle_fps(dict(flat)) or 30.0
    tracks = _layer_order(tl.bundle.get("parent", {}).get("config", {}).get("tracks") or [], internal.get("tracks") or [])
    track_defs = {str(t.get("id")): t for t in tracks}
    over_rows: dict[str, str] = {}
    for clip_order, clip in enumerate(internal.get("clips") or []):
        clip = copy.deepcopy(clip)
        app = clip.setdefault("app", {})
        if not isinstance(app, dict):
            app = clip["app"] = {}
        astrid = app.setdefault("astrid", {})
        if not isinstance(astrid, dict):
            astrid = app["astrid"] = {}
        astrid[_ORDER_MARKER] = clip_order
        s = _num(clip.get("at"))
        e = s + clip_duration(clip)
        render_end = min(e, end_s)
        # A nearest-ms wall can sit up to 0.5 ms after the source's frame-aligned
        # start. Treat starts within the same 1 ms tolerance as belonging to the
        # new chapter, so rounding the wall does not strand that chapter's first
        # clips on the previous side of the boundary.
        k = max((i for i, c in enumerate(chapters)
                 if c.start_ms / 1000.0 <= s + WALL_TOLERANCE + 1e-9), default=0)
        if s >= end_s - 1e-9:
            k, why = len(chapters) - 1, None  # never rendered; any chapter holds it, the last is the natural one
        else:
            why = "music" if tl._is_music(sid, clip) else (
                f"crosses the wall at {chapters[k + 1].start_ms / 1000:g} s into {chapters[k + 1].name}"
                if k + 1 < len(chapters) and e > chapters[k + 1].start_ms / 1000.0 + (1e-6 if tl._is_audio(sid, clip) else WALL_TOLERANCE) else None)
        cutter = _cutter_for(clip)
        can_cut = why != "music" and cutter is not None and not tl._is_audio(sid, clip)
        if can_cut and (why or e > end_s + 1e-9) and s < end_s:
            segments = []
            for chapter in chapters:
                lo = max(s, chapter.start_ms / 1000.0)
                hi = min(render_end, chapter.end_ms / 1000.0)
                if hi - lo > 1e-9:
                    segments.append((chapter, lo, hi))
            if len(segments) > 1:
                unsafe = _cut_safety_reason(clip, (lo for _, lo, _ in segments[1:]), fps, chapters)
                if unsafe:
                    can_cut = False
                    why = f"{why}; {unsafe}" if why else unsafe
            if can_cut and len(segments) > 1:
                source_id = str(clip.get("id"))
                shape = _piece_shape(clip)
                for index, (chapter, lo, hi) in enumerate(segments):
                    piece_id = _stable_id("chapter-piece", flat.get("timeline_id", ""), source_id,
                                          chapter.name, chapter.start_ms, index)
                    piece = cutter(clip, lo, hi, chapter, piece_id=piece_id, fps=fps) if cutter is _cut_sprite \
                        else cutter(clip, lo, hi, chapter, piece_id=piece_id)
                    app = piece.setdefault("app", {})
                    if not isinstance(app, dict):
                        app = piece["app"] = {}
                    astrid = app.setdefault("astrid", {})
                    if not isinstance(astrid, dict):
                        astrid = app["astrid"] = {}
                    marker = {"id": source_id, "index": index, "count": len(segments), "shape": shape,
                              "source_from_present": "from" in clip, "source_speed_present": "speed" in clip,
                              "source_speed": copy.deepcopy(clip.get("speed", 1.0))}
                    astrid[_PIECE_MARKER] = marker
                    chapter.clips.append(piece)
                continue
            if len(segments) == 1 and e > end_s + 1e-9:
                chapter, lo, hi = segments[0]
                chapter.clips.append(cutter(clip, lo, hi, chapter))
                continue
        if why:
            value = copy.deepcopy(clip)
            if e > end_s + 1e-9:  # the one occurrence used to clip it at the film's end; a tail would lengthen the render
                _set_length(value, end_s - s)
            if not can_cut and not tl._is_audio(sid, clip) and not tl._is_music(sid, clip):
                source_track = str(clip.get("track") or "picture")
                base_track = str(flat.get("placements", [{}])[0].get("track") or
                                 (flat.get("placements", [{}])[0].get("placement") or {}).get("track") or "picture")
                stored = flat.get(STRUCTURE_KEY) or {}
                matching_row = next((r for r in _rows(stored) if str(r.get("shot_id")) in (stored.get("shots") or {})
                                     and _shot_name(stored["shots"][str(r["shot_id"])]) == chapters[k].name), None)
                if matching_row is not None:
                    base_track = str(matching_row.get("track") or (matching_row.get("placement") or {}).get("track") or base_track)
                over_track = over_rows.get(base_track)
                if over_track is None:
                    over_track = next((track_id for track_id, definition in track_defs.items()
                                       if isinstance(definition.get("app"), Mapping)
                                       and isinstance(definition["app"].get("astrid"), Mapping)
                                       and definition["app"]["astrid"].get(_OVER_ROW_MARKER) ==
                                       {"sourceTrack": source_track, "row": base_track}), None)
                if over_track is None:
                    base_def = track_defs.get(base_track, {"id": base_track, "kind": "visual"})
                    source_def = track_defs.get(source_track, base_def)
                    candidate = f"{base_track}-over"
                    suffix = 2
                    while candidate in track_defs:
                        candidate = f"{base_track}-over-{suffix}"
                        suffix += 1
                    over_track = candidate
                    over_rows[base_track] = over_track
                    over_def = copy.deepcopy(dict(source_def))
                    over_def["id"] = over_track
                    app = over_def.setdefault("app", {})
                    if not isinstance(app, dict):
                        app = over_def["app"] = {}
                    astrid = app.setdefault("astrid", {})
                    if not isinstance(astrid, dict):
                        astrid = app["astrid"] = {}
                    astrid[_OVER_ROW_MARKER] = {"sourceTrack": source_track, "row": base_track}
                    at = next((i for i, t in enumerate(tracks) if str(t.get("id")) == base_track), 0)
                    tracks.insert(at, over_def)
                    track_defs[over_track] = over_def
                app = value.setdefault("app", {})
                if not isinstance(app, dict):
                    app = value["app"] = {}
                astrid = app.setdefault("astrid", {})
                if not isinstance(astrid, dict):
                    astrid = app["astrid"] = {}
                astrid[_OVER_MARKER] = {"track": source_track, "row": base_track}
                value["track"] = over_track
                label = str(value.get("name") or value.get("label") or value.get("asset") or "")
                over_clips.append((str(value.get("id")), label, _clip_kind(value), over_track))
            film.append(value)
            reasons[str(value.get("id"))] = why
        else:
            value = copy.deepcopy(clip)
            value["at"] = _r(max(0.0, s - chapters[k].start_ms / 1000.0))
            if e > end_s + 1e-9 and s < end_s and not tl._is_audio(sid, clip):
                _set_length(value, end_s - s)
            chapters[k].clips.append(value)
    return Plan(chapters, film, reasons, tracks,
                copy.deepcopy((internal.get("registry") or {}).get("assets") or {}), end_ms, over_clips)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _used(clips: Iterable[Mapping[str, Any]], keys: Mapping[str, Any]) -> set[str]:
    return {s for clip in clips for s in _strings(clip) if s in keys}


def _layer_order(parent_tracks: list[Mapping[str, Any]], shot_tracks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge one shot's order into film order, retaining complete definitions."""
    result = [copy.deepcopy(dict(track)) for track in parent_tracks]
    ids = {str(track.get("id")) for track in result}
    for index, track in enumerate(shot_tracks):
        key = str(track.get("id"))
        if key in ids:
            continue
        following = next((str(item.get("id")) for item in shot_tracks[index + 1:] if str(item.get("id")) in ids), None)
        if following is None:
            at = len(result)
        else:
            preceding = next((str(item.get("id")) for item in reversed(shot_tracks[:index])
                              if str(item.get("id")) in ids), None)
            at = (next(i + 1 for i, item in enumerate(result) if str(item.get("id")) == preceding)
                  if preceding is not None else next(i for i, item in enumerate(result) if str(item.get("id")) == following))
        result.insert(at, copy.deepcopy(dict(track)))
        ids.add(key)
    return result


def _stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}-{hashlib.sha256(chr(31).join(str(p) for p in parts).encode()).hexdigest()[:20]}"


def split(flat: Mapping[str, Any]) -> dict[str, Any]:
    """The stored form of a flat film: one shot per chapter, film-level clips on the parent. Not a chapter film: unchanged."""
    from astrid.sdk.timeline_editing import add_authoring_shot

    work = {k: v for k, v in flat.items() if k != STRUCTURE_KEY}
    planned = plan(flat)
    if planned is None:
        return copy.deepcopy(work)
    old = copy.deepcopy(dict(flat.get(STRUCTURE_KEY) or work))  # the stored form this film was read from
    prior_assets = copy.deepcopy(effective_parent_registry(old.get("parent") or {}).get("assets") or {})
    out = old
    out["parent"] = copy.deepcopy(work["parent"])
    parent, config = out["parent"], out["parent"].setdefault("config", {})
    if (parent.get("clips") or config.get("clips")):
        raise TimelineEditError("the film already has film-level clips next to a single shot; they cannot be merged into a restructure")
    config.pop("clips", None)
    parent["clips"] = planned.film_clips
    # Store the complete render order on the film, including tracks used only
    # inside chapter shots. This keeps later projections consistent.
    config["tracks"] = copy.deepcopy(planned.tracks)
    film_assets = _used(planned.film_clips, planned.assets)
    chapter_assets = [_used(c.clips, planned.assets) for c in planned.chapters]
    unused = set(planned.assets) - film_assets.union(*chapter_assets)
    registry = dict(parent.get("registry") or {}) if isinstance(parent.get("registry"), Mapping) else {}
    own = dict(registry.get("assets") or {})
    for key, value in prior_assets.items():
        if key in own:
            own[key] = _union_asset(own[key], value,
                                    f"the film registry and its prior form both define asset {key!r} differently")
        else:
            own[key] = copy.deepcopy(value)
    for key in planned.assets:
        if key in own:  # the film's copy becomes the union of its copy and the shots' (every copy of a key is the same)
            own[key] = _union_asset(own[key], planned.assets[key],
                                    f"the film registry and the shot both define asset {key!r} differently")
        elif key in film_assets | unused:
            own[key] = copy.deepcopy(planned.assets[key])
    registry["assets"] = own
    parent["registry"] = registry
    if isinstance(config.get("registry"), Mapping):
        config["registry"] = copy.deepcopy(registry)
    timeline_id = str(out.get("timeline_id"))
    by_name = {_shot_name(out["shots"][str(r["shot_id"])]): r for r in _rows(out) if str(r["shot_id"]) in out["shots"]}
    kept_rows = {str(r["shot_id"]): r for r in out.get("placements") or []}
    placements: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, chapter in enumerate(planned.chapters):
        prior = by_name.get(chapter.name)
        if prior and str(prior["shot_id"]) not in used_ids:
            sid = str(prior["shot_id"])
        else:
            sid = _stable_id("chapter-shot", timeline_id, chapter.name)
            salt = 0
            while sid in used_ids or sid in out["shots"]:
                salt += 1
                sid = _stable_id("chapter-shot", timeline_id, chapter.name, chapter.start_ms, salt)
        used_ids.add(sid)
        used_tracks = {str(c.get("track")) for c in chapter.clips}
        tracks = [copy.deepcopy(t) for t in planned.tracks if str(t.get("id")) in used_tracks]
        assets = {k: copy.deepcopy(planned.assets[k]) for k in planned.assets if k in chapter_assets[index]}
        if sid in out["shots"]:
            row = copy.deepcopy(kept_rows.get(sid) or next(iter(kept_rows.values())))
            row["shot_id"] = sid
            shot = out["shots"][sid]
            internal = shot.setdefault("internal_timeline", {})
        else:
            before = len(out["placements"])
            add_authoring_shot(out, shot_id=sid, name=chapter.name, occurrence_id=f"{sid}-occ", start_ms=chapter.start_ms)
            row = copy.deepcopy(out["placements"][before])
            del out["placements"][before:]
            internal = out["shots"][sid]["internal_timeline"]
        internal["clips"] = chapter.clips
        internal["tracks"] = tracks
        internal["registry"] = {**{k: v for k, v in (internal.get("registry") or {}).items() if k != "assets"}, "assets": assets}
        _set_row_start(row, chapter.start_ms / 1000.0)
        row["duration_ms"] = chapter.length_ms
        placements.append(row)
    out["placements"] = placements
    intent.set_chapters(out, [])  # the shots are the chapters now; the film never relies on both
    intent.migrate(out)
    return out


def walls_of(flat: Mapping[str, Any]) -> list[tuple[str, int, int]]:
    planned = plan(flat)
    return [(c.name, c.start_ms, c.length_ms) for c in planned.chapters] if planned else []


# ------------------------------------------------------------------ what the renderer receives

def renderer_input(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Project a STORED bundle exactly as a render does, normalised so only what the renderer sees remains."""
    from astrid.core.timeline.duration import timeline_render_duration_frames
    from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition

    rows = _rows(bundle)
    occurrences, shot_revisions, internals = [], [], []
    for row in rows:
        sid = str(row["shot_id"])
        occ = {k: copy.deepcopy(v) for k, v in row.items()}
        occ["shot_revision_id"] = f"r-{sid}"
        occurrences.append(occ)
        shot_revisions.append({"shot_id": sid, "revision_id": f"r-{sid}", "internal_timeline_revision_id": f"i-{sid}", "payload": {}})
        internals.append({"revision_id": f"i-{sid}", "payload": copy.deepcopy(bundle["shots"][sid].get("internal_timeline") or {})})
    payload = copy.deepcopy(bundle.get("parent") or {})
    payload["occurrences"] = occurrences
    projected = project_runtime_parent_composition(
        {"project_id": str(bundle.get("project_id")), "timeline_id": str(bundle.get("timeline_id")), "revision_id": "x", "payload": payload},
        shot_revisions=shot_revisions, internal_timeline_revisions=internals)
    assets = projected.registry.get("assets") or {}
    projected_tracks = projected.config.get("tracks") or []
    clips_by_track: dict[str, list[Mapping[str, Any]]] = {}
    for clip in projected.config["clips"]:
        clips_by_track.setdefault(str(clip.get("track")), []).append(clip)
    logical_tracks: dict[str, str] = {}
    normalized_tracks = []
    seen_tracks: set[str] = set()
    for raw_track in projected_tracks:
        track = copy.deepcopy(raw_track)
        app = track.get("app") if isinstance(track.get("app"), Mapping) else {}
        group = app.get("group") if isinstance(app, Mapping) else None
        grouped = isinstance(group, Mapping)
        logical_id = str(group.get("sourceTrack")) if grouped else str(track.get("id"))
        logical_tracks[str(track.get("id"))] = logical_id
        if not clips_by_track.get(str(track.get("id"))):
            continue
        track["id"] = logical_id
        if grouped:
            app = dict(app)
            app.pop("group", None)
            if app:
                track["app"] = app
            else:
                track.pop("app", None)
        if logical_id not in seen_tracks:
            normalized_tracks.append(track)
            seen_tracks.add(logical_id)
    audio_tracks = {str(t.get("id")) for t in normalized_tracks if str(t.get("kind")) in AUDIO_KINDS} | AUDIO_KINDS
    per_track: dict[str, list[tuple[float, int, Any]]] = {}
    render_clips: list[tuple[dict[str, Any], float, float, bool]] = []
    for index, original in enumerate(projected.config["clips"]):
        clip = copy.deepcopy(original)
        clip["track"] = logical_tracks.get(str(clip.get("track")), str(clip.get("track")))
        for key in ("id", "shot_id", "shot_occurrence_id", "shot_name"):
            clip.pop(key, None)
        app = clip.get("app")
        if isinstance(app, dict):
            app.pop("astrid_shot_composition", None)
            astrid = app.get("astrid")
            if isinstance(astrid, dict):
                astrid.pop(_PIECE_MARKER, None)
                astrid.pop(_OVER_MARKER, None)
                astrid.pop(_ORDER_MARKER, None)
                if not astrid:
                    app.pop("astrid")
            if not app:
                clip.pop("app")
        for selector in ("asset", "asset_id"):
            if isinstance(clip.get(selector), str):
                clip[selector] = {"key": clip[selector].split(":")[-1], "entry": assets.get(clip[selector])}
        duration = _r(clip_duration(clip))
        clip.pop("hold", None)
        clip.pop("to", None) if "hold" in original else None
        if "to" in clip:
            clip["to"] = _r(_num(clip["to"]))
        clip["at"] = _r(_num(clip.get("at")))
        clip.setdefault("volume", 1.0)
        audio = str(clip.get("track")) in audio_tracks
        render_clips.append((clip, clip["at"], duration, audio))
        per_track.setdefault(str(clip.get("track")), []).append((clip["at"], index, (json.dumps(clip, sort_keys=True, default=str), duration, audio)))
    fps = bundle_fps(dict(bundle)) or 30.0
    frames = timeline_render_duration_frames(projected.config, fps)
    active_frames = []
    for frame in range(frames):
        t = frame / fps
        active = []
        for clip, start, duration, audio in render_clips:
            if audio or start > t + 1e-9 or t >= start + duration - 1e-9:
                continue
            elapsed = max(0.0, t - start)
            speed = _num(clip.get("speed"), 1.0) or 1.0
            source_at = _r(_num(clip.get("from")) + elapsed * speed) if clip.get("from") is not None else (
                _r(elapsed * speed) if _clip_kind(clip) in {"media", "video"} else None)
            active.append((str(clip.get("track")), json.dumps(clip, sort_keys=True, default=str),
                           _r(elapsed), source_at, speed))
        active_frames.append(tuple(active))
    return {"tracks": {str(track["id"]): track for track in normalized_tracks},
            "clips": {t: [row for _, _, row in sorted(v, key=lambda r: (r[0], r[1]))] for t, v in per_track.items()},
            "frames": frames, "active_frames": active_frames}


def differences(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    def migrated(bundle: Mapping[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(dict(bundle))
        intent.migrate(value)  # legacy and namespaced intent are the same content
        return value

    left, right = migrated(before), migrated(after)
    stack_a, stack_b = _active_stack(left), _active_stack(right)
    out = []
    if stack_a != stack_b:
        frames = max(len(stack_a), len(stack_b))
        frame = next((i for i in range(frames)
                      if (stack_a[i] if i < len(stack_a) else ()) != (stack_b[i] if i < len(stack_b) else ())), 0)
        out.append(f"active visual stacking / track order differs at frame {frame}: "
                   f"{stack_a[frame] if frame < len(stack_a) else ()} vs "
                   f"{stack_b[frame] if frame < len(stack_b) else ()}")
    a, b = renderer_input(flatten(left)), renderer_input(flatten(right))
    if a["tracks"] != b["tracks"]:
        out.append(f"track order or definitions differ: {a['tracks']} vs {b['tracks']}")
    if a["frames"] != b["frames"]:
        out.append(f"duration differs: {a['frames']} vs {b['frames']} frames")
    if a["active_frames"] != b["active_frames"]:
        frame = next((i for i, (x, y) in enumerate(zip(a["active_frames"], b["active_frames"])) if x != y),
                     min(len(a["active_frames"]), len(b["active_frames"])))
        out.append(f"projected active clip state differs at frame {frame}")
    for track in sorted(set(a["clips"]) | set(b["clips"])):
        x, y = a["clips"].get(track, []), b["clips"].get(track, [])
        # a clip clipped at a chapter wall may differ by under a millisecond when it is picture (frames are 33 ms);
        # sound must match to a microsecond
        same = len(x) == len(y) and all(p[0] == q[0] and abs(p[1] - q[1]) <= (1e-6 if p[2] else WALL_TOLERANCE) for p, q in zip(x, y))
        if not same:
            only_x = [r[0][:300] for r in x if r[0] not in {z[0] for z in y}][:2]
            only_y = [r[0][:300] for r in y if r[0] not in {z[0] for z in x}][:2]
            out.append(f"track {track}: {len(x)} vs {len(y)} clips; first only-before {only_x}; only-after {only_y}"
                       if only_x or only_y else f"track {track}: same clips, different order")
    return out


def _active_stack(bundle: Mapping[str, Any]) -> list[tuple[tuple[str, str, str], ...]]:
    """Capture actual projected visual order at each output frame before flattening groups."""
    from astrid.core.timeline.duration import timeline_render_duration_frames
    from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition

    occurrences, shot_revisions, internals = [], [], []
    for row in _rows(bundle):
        sid = str(row["shot_id"])
        occ = copy.deepcopy(row)
        occ["shot_revision_id"] = f"r-{sid}"
        occurrences.append(occ)
        shot_revisions.append({"shot_id": sid, "revision_id": f"r-{sid}",
                               "internal_timeline_revision_id": f"i-{sid}", "payload": {}})
        internals.append({"revision_id": f"i-{sid}",
                          "payload": copy.deepcopy(bundle["shots"][sid].get("internal_timeline") or {})})
    payload = copy.deepcopy(bundle.get("parent") or {})
    parent_track_defs = {str(track.get("id")): track
                         for track in (payload.get("config") or {}).get("tracks") or []}
    payload["occurrences"] = occurrences
    projected = project_runtime_parent_composition(
        {"project_id": str(bundle.get("project_id")), "timeline_id": str(bundle.get("timeline_id")),
         "revision_id": "x", "payload": payload},
        shot_revisions=shot_revisions, internal_timeline_revisions=internals)
    fps = bundle_fps(dict(bundle)) or 30.0
    frame_count = timeline_render_duration_frames(projected.config, fps)
    track_defs = {str(track.get("id")): track for track in projected.config.get("tracks") or []}
    ordered_ids = [str(track.get("id")) for track in projected.config.get("tracks") or []]
    rows_by_track: dict[str, list[tuple[str, str, str, float, float]]] = {}
    for clip in projected.config.get("clips") or []:
        track_id = str(clip.get("track"))
        track = track_defs.get(track_id, {})
        if track.get("kind") == "audio" or track_id in AUDIO_KINDS:
            continue
        app = track.get("app") if isinstance(track.get("app"), Mapping) else {}
        group = app.get("group") if isinstance(app, Mapping) else None
        logical_track = str(group.get("sourceTrack")) if isinstance(group, Mapping) else track_id
        clip_app = clip.get("app") if isinstance(clip.get("app"), Mapping) else {}
        astrid = clip_app.get("astrid") if isinstance(clip_app, Mapping) else {}
        over = astrid.get(_OVER_MARKER) if isinstance(astrid, Mapping) else None
        if isinstance(over, Mapping) and over.get("track") is not None:
            logical_track = str(over["track"])
        definition = copy.deepcopy(dict(track))
        definition["id"] = logical_track
        definition_app = definition.get("app")
        if isinstance(definition_app, dict):
            definition_app.pop("group", None)
            definition_astrid = definition_app.get("astrid")
            if isinstance(definition_astrid, dict):
                definition_astrid.pop(_OVER_ROW_MARKER, None)
                if not definition_astrid:
                    definition_app.pop("astrid", None)
            if not definition_app:
                definition.pop("app", None)
        definition_signature = json.dumps(definition, sort_keys=True, default=str)
        marker = astrid.get(_PIECE_MARKER) if isinstance(astrid, Mapping) else None
        provenance = clip_app.get("astrid_shot_composition")
        source_id = marker.get("id") if isinstance(marker, Mapping) else (
            provenance.get("source_clip_id") if isinstance(provenance, Mapping) else clip.get("id"))
        start_frame = _js_round(_num(clip.get("at")) * fps)
        duration_frames = max(1, _js_round(clip_duration(clip) * fps))
        rows_by_track.setdefault(track_id, []).append(
            (logical_track, str(source_id), definition_signature, start_frame, start_frame + duration_frames))
    stacks = []
    for frame in range(frame_count):
        t = frame / fps
        active = []
        for occurrence in occurrences:
            start = _row_start(occurrence) / 1000.0
            end = start + _num(occurrence.get("duration_ms")) / 1000.0
            if start <= t + 1e-9 and t < end - 1e-9:
                row_track = str(occurrence.get("track") or (occurrence.get("placement") or {}).get("track") or "picture")
                row_definition = parent_track_defs.get(row_track)
                active.append((f"@row:{row_track}", "", json.dumps(row_definition, sort_keys=True, default=str)
                               if row_definition is not None else "missing"))
        for track_id in ordered_ids:
            for logical_track, source_id, definition_signature, start_frame, end_frame in rows_by_track.get(track_id, []):
                if start_frame <= frame < end_frame:
                    active.append((logical_track, source_id, definition_signature))
        stacks.append(tuple(active))
    return stacks


def assert_equivalent(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    problems = differences(before, after)
    if problems:
        raise TimelineEditError("restructure refused: the renderer would receive something different:\n  " + "\n  ".join(problems))


def describe(flat: Mapping[str, Any]) -> list[str]:
    """The dry-run report: walls, clips per chapter, film-level clips."""
    planned = plan(flat)
    if planned is None:
        return ["not a chapter film: it needs at least two chapter labels (nothing to restructure)"]
    lines = ["walls (chapter, start, length)"]
    for c in planned.chapters:
        lines.append(f"  {c.name:<28} {c.start_ms / 1000:9.3f} s  {c.length_ms / 1000:8.3f} s  {len(c.clips):4d} clips")
    lines.append(f"film-level clips ({len(planned.film_clips)}): stored on the film, not in a chapter")
    for clip in planned.film_clips:
        s = _num(clip.get("at"))
        lines.append(f"  {str(clip.get('id')):<28} {clip.get('track')!s:<16} {s:9.3f}-{s + clip_duration(clip):.3f} s  {planned.reasons[str(clip.get('id'))]}")
    if planned.over_clips:
        lines.append("uncuttable visual clips on rows above their shot occurrence:")
        for clip_id, name, kind, track in planned.over_clips:
            suffix = f" ({name})" if name else ""
            lines.append(f"  {clip_id}{suffix} [{kind}] -> {track}")
    lines.append(f"total: {sum(len(c.clips) for c in planned.chapters)} chapter clips + {len(planned.film_clips)} film-level clips, "
                 f"{len(planned.chapters)} chapter shots, {planned.end_ms / 1000:.3f} s")
    return lines


def check_restructure(tl: Any) -> tuple[list[str], list[str], dict[str, Any]]:
    """For a checkout: (the dry-run report, the differences the renderer would see, the stored candidate)."""
    tl.notes.extend(tl._sync_film_length())
    flat = tl.document()
    after = split(flat)
    before = flat.get(STRUCTURE_KEY) or {k: v for k, v in flat.items()}
    return describe(flat), differences(before, after), after
