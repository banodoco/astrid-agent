"""Chapters as stored shots, one flat timeline for editing.

The edit engine works on ONE shot whose clip times are film seconds. This module converts at the two
storage boundaries:

* ``flatten(stored)``: a stored film (one shot per chapter, plus film-level clips, registry and track order on the
  parent) becomes the flat working shot. The stored form is kept under ``structure_base``.
* ``split(flat)``: the flat shot becomes the stored form. Each chapter is a real shot (a split film's chapters are
  its shot placements, anchored to cuts in the flat form; a one-shot film's come from its labels, once, at restructure); music and any clip that crosses a chapter wall (by more than 1 ms) is a film-level (parent) clip.

``renderer_input`` / ``assert_equivalent`` project a stored form exactly as the renderer does and prove two forms
hand the renderer the same thing, so a restructure that would change a frame or a sample refuses to publish.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from astrid.core.timeline.cuts import bundle_fps, clip_duration
from astrid.core.timeline.parent_registry import effective_parent_registry
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import AUDIO_KINDS, TimelineEditError, _num, _r
from astrid.sdk.timeline.doc import _row_start, _set_length, _set_row_start

STRUCTURE_KEY = "structure_base"
WALL_TOLERANCE = 0.001  # a clip crossing a wall by no more than 1 ms stays in its chapter


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


def _flat_side(view: Mapping[str, Any]) -> dict[str, Any]:
    parent = copy.deepcopy(dict(view["parent"]))
    config = parent.setdefault("config", {})
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
    base = _flat_side(_view(stored, base=True)) if _rows(stored, base=True) else cur
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
    for clip in internal.get("clips") or []:
        s = _num(clip.get("at"))
        e = s + clip_duration(clip)
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
        if why:
            value = copy.deepcopy(clip)
            if e > end_s + 1e-9:  # the one occurrence used to clip it at the film's end; a tail would lengthen the render
                _set_length(value, end_s - s)
            film.append(value)
            reasons[str(value.get("id"))] = why
        else:
            value = copy.deepcopy(clip)
            value["at"] = _r(max(0.0, s - chapters[k].start_ms / 1000.0))
            chapters[k].clips.append(value)
    return Plan(chapters, film, reasons, _layer_order(tl.bundle.get("parent", {}).get("config", {}).get("tracks") or [], internal.get("tracks") or []),
                copy.deepcopy((internal.get("registry") or {}).get("assets") or {}), end_ms)


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
    audio_tracks = {str(t.get("id")) for t in projected.config.get("tracks") or [] if str(t.get("kind")) in AUDIO_KINDS} | AUDIO_KINDS
    per_track: dict[str, list[tuple[float, int, Any]]] = {}
    for index, original in enumerate(projected.config["clips"]):
        clip = copy.deepcopy(original)
        for key in ("id", "shot_id", "shot_occurrence_id", "shot_name"):
            clip.pop(key, None)
        app = clip.get("app")
        if isinstance(app, dict):
            app.pop("astrid_shot_composition", None)
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
        per_track.setdefault(str(clip.get("track")), []).append((clip["at"], index, (json.dumps(clip, sort_keys=True, default=str), duration, audio)))
    fps = bundle_fps(dict(bundle)) or 30.0
    return {"tracks": [t.get("id") for t in projected.config.get("tracks") or []],
            "clips": {t: [row for _, _, row in sorted(v, key=lambda r: (r[0], r[1]))] for t, v in per_track.items()},
            "frames": timeline_render_duration_frames(projected.config, fps)}


def differences(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    def migrated(bundle: Mapping[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(dict(bundle))
        intent.migrate(value)  # legacy and namespaced intent are the same content
        return value

    a, b = renderer_input(migrated(before)), renderer_input(migrated(after))
    out = []
    if a["tracks"] != b["tracks"]:
        out.append(f"track order differs: {a['tracks']} vs {b['tracks']}")
    if a["frames"] != b["frames"]:
        out.append(f"duration differs: {a['frames']} vs {b['frames']} frames")
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
        lines.append(f"  {str(clip.get('id')):<28} {clip.get('track')!s:<8} {s:9.3f}-{s + clip_duration(clip):.3f} s  {planned.reasons[str(clip.get('id'))]}")
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
