"""One definition of a picture cut, shared by every reader of a timeline.

``timelines show`` (the cut table), ``timelines visualize`` (one contact tile
per cut, ``--sample cuts``, ``--view motion --cut N``), ``timelines lint`` and
``editorial.pacing`` all count cuts with :func:`picture_cuts`, so "cut 17"
means the same thing in every view.

**A cut is one clip on the shot's picture-bed track.** The bed is ``plate``
when the shot has one, otherwise the visual track whose clips cover most of
the shot. A stretch of a shot with no bed clip is a cut with no picture clip
(``clip`` is ``None``). Every other visual clip overlapping the cut is a
*layer* over it; a layer edge is never a cut. Consecutive bed clips that
share ``app.sequence`` (a time-lapse or stepped animation the builder lays
out clip by clip) are ONE cut; ``cut["sequence"]`` lists its steps.

The function is pure and representation-neutral: callers adapt their own
timeline shape into *occurrences* (one placed shot each) whose *spans* are
clips with absolute timeline seconds. Two adapters are provided:
:func:`occurrences_from_bundle` (the authoring bundle that ``show`` and
``pacing`` read) and :func:`occurrences_from_snapshot` (the flattened clips a
visualize capture snapshot carries).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .astrid_ns import read_app

__all__ = [
    "AUDIO_KINDS",
    "PICTURE_BED_TRACKS",
    "DELIBERATE_KEYS",
    "clip_duration",
    "is_audio_span",
    "is_deliberate",
    "picture_bed",
    "picture_cuts",
    "occurrences_from_bundle",
    "occurrences_from_snapshot",
    "cut_sample_time",
    "find_cut",
    "sequence_id",
]

AUDIO_KINDS = frozenset({"audio", "voice", "voiceover", "vo", "music", "sound", "sfx"})
PICTURE_BED_TRACKS = ("plate",)
DELIBERATE_KEYS = ("deliberate_hold", "deliberate")
# Seconds a layer needs after its entrance before a still of it is
# representative (stamp 2 frames, slide-in 8, type slide-up 8, connector 12).
SETTLE_SECONDS = 0.4
_EPS = 1e-6


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping) and "numerator" in value:
        denominator = _num(value.get("denominator"), 1.0) or 1.0
        return _num(value.get("numerator")) / denominator
    if isinstance(value, (list, tuple)) and len(value) == 2:
        denominator = _num(value[1], 1.0) or 1.0
        return _num(value[0]) / denominator
    return default


def clip_duration(clip: Mapping[str, Any]) -> float:
    """Timeline seconds a clip occupies (``hold``, else ``to - from``, else ``duration``; over speed)."""
    speed = _num(clip.get("speed"), 1.0) or 1.0
    if clip.get("hold") is not None:
        length = _num(clip.get("hold"))
    elif clip.get("to") is not None:
        length = _num(clip.get("to")) - _num(clip.get("from"))
    else:
        length = _num(clip.get("duration"))
    return max(0.0, length / speed)


def is_deliberate(clip: Mapping[str, Any] | None) -> bool:
    """True when the clip is marked as a deliberate hold (``app`` or ``params``)."""
    if not isinstance(clip, Mapping):
        return False
    params = _map(clip.get("params"))
    app = {**_map(params.get("app")), **read_app(clip)}
    return any(bool(app.get(key) or params.get(key)) for key in DELIBERATE_KEYS)


def sequence_id(clip: Mapping[str, Any] | None) -> str | None:
    """``app.sequence`` of a clip: picture clips sharing it in a row are one cut."""
    if not isinstance(clip, Mapping):
        return None
    value = read_app(clip).get("sequence")
    if isinstance(value, Mapping):
        value = value.get("id")
    return str(value) if isinstance(value, (str, int)) and str(value) else None


def is_audio_span(span: Mapping[str, Any]) -> bool:
    return bool(span.get("audio"))


def _span(clip: Mapping[str, Any], start: float, kinds: Mapping[str, str], *, kind: str | None = None) -> dict[str, Any]:
    track = str(clip.get("track") or "")
    track_kind = (kind or kinds.get(track) or "").lower()
    audio = track_kind in AUDIO_KINDS or track in AUDIO_KINDS
    return {
        "clip": clip,
        "id": str(clip.get("id") or ""),
        "start": start,
        "end": start + clip_duration(clip),
        "track": track,
        "audio": audio,
        "type": str(clip.get("clipType") or clip.get("type") or clip.get("kind") or "media"),
    }


def picture_bed(spans: Sequence[Mapping[str, Any]], track_order: Sequence[str] = ()) -> str | None:
    """The picture-bed track: ``plate`` if present, else the visual track covering most time."""
    visual = [span for span in spans if not span["audio"] and span["end"] - span["start"] > 0]
    tracks = {str(span["track"]) for span in visual}
    for preferred in PICTURE_BED_TRACKS:
        if preferred in tracks:
            return preferred
    if not tracks:
        return None
    coverage = {track: sum(s["end"] - s["start"] for s in visual if s["track"] == track) for track in tracks}
    order = {track: index for index, track in enumerate(track_order)}
    return max(sorted(tracks), key=lambda track: (coverage[track], -order.get(track, len(order))))


def picture_cuts(occurrences: Iterable[Mapping[str, Any]], *, fps: float = 30.0) -> list[dict[str, Any]]:
    """Every picture cut in timeline order, numbered from 1.

    Each occurrence is ``{occurrence_id, shot_id, name, start, end, spans,
    track_order}`` where ``spans`` are clips with absolute ``start``/``end``
    seconds (see :func:`occurrences_from_bundle`). A cut is
    ``{index, start, end, duration, occurrence_id, shot_id, shot, clip,
    clip_id, track, type, layers, deliberate_hold}``; ``layers`` are the visual
    spans over it (each with its absolute ``start``/``end``).
    """
    half = 0.5 / (fps or 30.0)
    cuts: list[dict[str, Any]] = []
    for occurrence in sorted(occurrences, key=lambda item: (float(item["start"]), str(item.get("occurrence_id")))):
        start, end = float(occurrence["start"]), float(occurrence["end"])
        spans = list(occurrence.get("spans") or [])
        bed = picture_bed(spans, occurrence.get("track_order") or ())
        visual = [s for s in spans if not s["audio"] and s["end"] - s["start"] > 0]
        bed_spans = sorted((s for s in visual if s["track"] == bed), key=lambda s: (s["start"], s["id"]))
        windows: list[tuple[float, float, Mapping[str, Any] | None, list[Mapping[str, Any]]]] = []
        cursor = start
        for span in bed_spans:
            sequence = sequence_id(span["clip"])
            previous = windows[-1] if windows else None
            if (sequence and previous is not None and previous[2] is not None
                    and sequence_id(previous[2]["clip"]) == sequence and span["start"] <= previous[1] + half):
                # One editorial cut: consecutive picture clips of one sequence
                # (a time-lapse or stepped animation built clip by clip).
                windows[-1] = (previous[0], max(previous[1], span["end"]), previous[2], previous[3] + [span])
                cursor = max(cursor, span["end"])
                continue
            if span["start"] > cursor + half:
                windows.append((cursor, span["start"], None, []))
            windows.append((span["start"], span["end"], span, [span]))
            cursor = max(cursor, span["end"])
        if not bed_spans or cursor < end - half:
            windows.append((cursor, end, None, []))
        for win_start, win_end, base, members in windows:
            if win_end - win_start <= _EPS:
                continue
            own = {id(member) for member in members}
            layers = [
                s for s in visual
                if id(s) not in own and s["end"] > win_start + half and s["start"] < win_end - half
            ]
            clip = base["clip"] if base else None
            if len(members) > 1:
                sequence = {
                    "id": sequence_id(clip),
                    "steps": len(members),
                    "clip_ids": [member["id"] for member in members],
                    "starts": [round(member["start"], 6) for member in members],
                }
            else:
                sequence = None
            cuts.append({
                "start": round(win_start, 6),
                "end": round(win_end, 6),
                "duration": round(win_end - win_start, 6),
                "occurrence_id": str(occurrence.get("occurrence_id") or ""),
                "shot_id": str(occurrence.get("shot_id") or ""),
                "shot": str(occurrence.get("name") or occurrence.get("shot_id") or ""),
                "clip": clip,
                "clip_id": base["id"] if base else None,
                "cut_id": (str(read_app(clip).get("cut") or "") or None) if clip else None,
                "track": bed,
                "type": base["type"] if base else None,
                "layers": layers,
                "deliberate_hold": is_deliberate(clip),
                "sequence": sequence,
            })
    cuts.sort(key=lambda cut: (cut["start"], cut["end"]))
    for index, cut in enumerate(cuts, start=1):
        cut["index"] = index
    return cuts


def find_cut(cuts: Sequence[Mapping[str, Any]], selector: Any) -> Mapping[str, Any]:
    """Resolve ``17`` (1-based number), a clip id (or its suffix) or ``@SECONDS`` to one cut."""
    text = str(selector).strip()
    if not cuts:
        raise ValueError("timeline has no picture cuts")
    if text.startswith("@"):
        seconds = float(text[1:])
        for cut in cuts:
            if cut["start"] - _EPS <= seconds < cut["end"] - _EPS:
                return cut
        raise ValueError(f"no cut covers {seconds:g} s (timeline ends at {cuts[-1]['end']:g} s)")
    if text.isdigit():
        number = int(text)
        if 1 <= number <= len(cuts):
            return cuts[number - 1]
        raise ValueError(f"cut {number} does not exist; this timeline has cuts 1–{len(cuts)}")
    for cut in cuts:
        clip_ids = [cut.get("clip_id") or ""] + list(_map(cut.get("sequence")).get("clip_ids") or [])
        if any(clip_id == text or clip_id.endswith(":" + text) for clip_id in clip_ids):
            return cut
    raise ValueError(f"no cut matches {selector!r}; pass a cut number 1–{len(cuts)}, a picture clip id or @SECONDS")


def cut_sample_time(cut: Mapping[str, Any], fps: float = 30.0, *, settle: float = SETTLE_SECONDS) -> float:
    """The representative still of a cut: when the most layers are on screen and settled.

    Candidates are the midpoint and each layer entrance plus ``settle``; the
    winner shows the most layers (ties go to the candidate nearest the
    midpoint). Always inside the cut, at least one frame before its end.
    """
    start, end = float(cut["start"]), float(cut["end"])
    frame = 1.0 / (fps or 30.0)
    last = max(start, end - frame)
    mid = min(last, start + (end - start) / 2)
    candidates = {round(mid, 6)}
    for layer in cut.get("layers") or ():
        if layer["start"] > start + frame:
            candidates.add(round(min(last, layer["start"] + settle), 6))

    def visible(t: float) -> int:
        return sum(1 for layer in cut.get("layers") or () if layer["start"] <= t + _EPS and layer["end"] > t + _EPS)

    return max(sorted(candidates), key=lambda t: (visible(t), -abs(t - mid)))


# ---------------------------------------------------------------- adapters

def _track_kinds(tracks: Any) -> dict[str, str]:
    return {
        str(track.get("id")): str(track.get("kind") or track.get("type") or "").lower()
        for track in _list(tracks)
        if isinstance(track, Mapping) and track.get("id") is not None
    }


def bundle_fps(bundle: Mapping[str, Any]) -> float:
    for parent in (_map(bundle.get("parent")), _map(bundle.get("base_parent_payload"))):
        canvas = _map(_map(_map(_map(parent.get("config")).get("theme_overrides")).get("visual")).get("canvas"))
        fps = _num(canvas.get("fps"), 0.0)
        if fps > 0:
            return fps
    return 30.0


def _shot_name(shot: Mapping[str, Any], shot_id: str) -> str:
    payload = _map(shot.get("payload")) or _map(shot.get("base_payload"))
    metadata = _map(payload.get("metadata"))
    return str(payload.get("name") or metadata.get("name") or shot_id)


def occurrences_from_bundle(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Adapt an authoring bundle (parent placements → shots → internal timelines).

    Placements store milliseconds and clips store shot-relative seconds; both
    become absolute timeline seconds. Each occurrence also carries ``shot``
    (the raw shot record) and ``registry`` (the shot's asset registry).
    """
    shots = _map(bundle.get("shots"))
    placements = [_map(item) for item in _list(bundle.get("placements")) if isinstance(item, Mapping)]
    placements.sort(key=lambda row: _num(row.get("at_ms", _map(row.get("placement")).get("start_ms"))))
    occurrences: list[dict[str, Any]] = []
    for placement in placements:
        shot_id = str(placement.get("shot_id") or "")
        occurrence_id = str(placement.get("occurrence_id") or shot_id)
        start = _num(placement.get("at_ms", _map(placement.get("placement")).get("start_ms"))) / 1000.0
        length = _num(placement.get("duration_ms")) / 1000.0
        shot = _map(shots.get(shot_id))
        internal = _map(shot.get("internal_timeline")) or _map(shot.get("base_internal_timeline"))
        kinds = _track_kinds(internal.get("tracks"))
        clips = [_map(clip) for clip in _list(internal.get("clips")) if isinstance(clip, Mapping)]
        spans = [_span(clip, start + _num(clip.get("at")), kinds) for clip in clips]
        end = start + length if length > 0 else max([s["end"] for s in spans] or [start])
        occurrences.append({
            "occurrence_id": occurrence_id,
            "shot_id": shot_id,
            "name": _shot_name(shot, shot_id),
            "start": start,
            "end": end,
            "spans": spans,
            "track_order": [str(_map(track).get("id")) for track in _list(internal.get("tracks"))],
            "shot": shot,
            "registry": _map(_map(internal.get("registry")).get("assets")),
        })
    return occurrences


def occurrences_from_snapshot(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Adapt a visualize snapshot (flattened clips with absolute ``at``).

    Clips are grouped by ``occurrence_id`` (legacy ``shot_occurrence_id``);
    occurrence windows and names come from ``snapshot['occurrences']`` when
    present. A snapshot without occurrences is one occurrence spanning every
    clip, so a plain timeline still gets plate-track cuts.
    """
    tracks = snapshot.get("tracks") or _map(snapshot.get("config")).get("tracks")
    kinds = _track_kinds(tracks)
    track_order = [str(_map(track).get("id")) for track in _list(tracks)]
    groups: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, tuple[str, str]] = {}
    for clip in _list(snapshot.get("clips")):
        if not isinstance(clip, Mapping):
            continue
        key = str(clip.get("occurrence_id") or clip.get("shot_occurrence_id") or "")
        clip_kind = str(clip.get("kind") or "").lower()
        span_kind = "audio" if clip_kind in AUDIO_KINDS else None
        groups.setdefault(key, []).append(_span(clip, _num(clip.get("at")), kinds, kind=span_kind))
        if key not in names:
            names[key] = (str(clip.get("shot_id") or ""), str(clip.get("shot_name") or clip.get("shot_id") or ""))
    windows = {
        str(item.get("occurrence_id")): item
        for item in _list(snapshot.get("occurrences"))
        if isinstance(item, Mapping) and item.get("occurrence_id") is not None
    }
    occurrences: list[dict[str, Any]] = []
    for key, spans in groups.items():
        window = _map(windows.get(key))
        start = _num(window.get("start"), min(s["start"] for s in spans)) if window else min(s["start"] for s in spans)
        end = _num(window.get("end"), max(s["end"] for s in spans)) if window else max(s["end"] for s in spans)
        shot_id, name = names[key]
        occurrences.append({
            "occurrence_id": key,
            "shot_id": str(window.get("shot_id") or shot_id),
            "name": str(window.get("shot_name") or name or shot_id),
            "start": start,
            "end": end,
            "spans": spans,
            "track_order": track_order,
        })
    return occurrences
