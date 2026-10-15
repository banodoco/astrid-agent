"""An editor's view of a timeline: one row per picture cut.

Each row answers "what is on screen from when to when, and what is being
said": timeline seconds and timecode, the shot (chapter) name, the picture
clip, the layers over it, and the words spoken under it.

These are pure functions over an *authoring bundle*: the JSON that
``timeline_document.py checkout`` writes and ``client.timelines.open_bundle``
returns. Nothing here reads or writes the Runtime.

Definitions (shared by ``timelines show`` and ``timelines diff``):

- **Times** are timeline seconds (the numbers ``--range`` and ``--at`` take).
  Timecode is ``HH:MM:SS:FF`` at the canvas fps. Placements store
  milliseconds and clips store shot-relative seconds; both are converted here.
- **A cut** is one clip on a shot's picture-bed track: ``plate`` when the
  shot has one, otherwise the visual track whose clips cover most of the
  shot. A shot with no picture clip is one cut. Other visual clips are
  *layers* over the cut. The definition lives in
  ``astrid.core.timeline.cuts.picture_cuts`` and is shared with
  ``timelines visualize``, ``timelines lint`` and ``editorial.pacing``.
- **Words** come from VO clip ``app.words`` entries ``[start, end, text]``.
  Timing-only entries ``[start, end]`` are matched to the shot's narration
  text binding when the word counts agree exactly; otherwise rows show the
  VO clip ids and ``word_source`` says why.
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Mapping, Sequence

from astrid.core.timeline.astrid_ns import film_intent, read_app

__all__ = [
    "build_cut_table",
    "filter_rows",
    "render_cut_table",
    "render_summary",
    "resolve_shot",
    "parse_seconds",
    "count_clips",
    "base_bundle",
    "diff_bundles",
    "render_diff",
    "timecode",
]

AUDIO_KINDS = {"audio", "voice", "voiceover", "vo", "music", "sound"}
PICTURE_BED_TRACKS = ("plate",)
TEXT_PARAM_KEYS = ("text", "title")
_EPS = 1e-6


# ----------------------------------------------------------------- helpers

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


def resolve_shot(table: Mapping[str, Any], shot: str) -> set[str]:
    """Shot ids for ``--shot``: a 1-based chapter number, a shot id, an occurrence id or a name."""
    wanted = str(shot).strip().lower()
    shots = list(table.get("shots") or [])
    ids: set[str] = set()
    if wanted.isdigit() and 1 <= int(wanted) <= len(shots):
        ids.add(shots[int(wanted) - 1]["shot_id"])
    for item in shots:
        if wanted in {item["shot_id"].lower(), item["occurrence_id"].lower(), item["name"].lower()}:
            ids.add(item["shot_id"])
    if not ids:
        names = ", ".join(f"{item['name']!r} ({item['shot_id']})" for item in shots)
        raise ValueError(f"no shot matches {shot!r}; shots: {names}")
    return ids


def parse_seconds(value: str) -> float:
    """``62.5``, ``1:02`` or ``1:02.5`` as timeline seconds."""
    text = str(value).strip()
    try:
        if ":" in text:
            minutes, rest = text.split(":", 1)
            seconds = float(minutes) * 60 + float(rest)
        else:
            seconds = float(text)
    except ValueError as exc:
        raise ValueError(f"time must be seconds or m:ss, got {value!r}") from exc
    if seconds < 0:
        raise ValueError("time must not be negative")
    return seconds


def count_clips(bundle: Mapping[str, Any]) -> int:
    """Every authored clip in the bundle: parent clips plus each shot's internal clips."""
    total = len(_list(_map(bundle.get("parent")).get("clips")))
    for shot in _map(bundle.get("shots")).values():
        total += len(_list(_internal(_map(shot)).get("clips")))
    return total


def _is_rational(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "numerator" in value
    return (
        isinstance(value, (list, tuple)) and len(value) == 2
        and all(isinstance(x, int) and not isinstance(x, bool) for x in value) and value[1] != 0
    )


def page_rows(rows, *, page=None, page_size=None):
    """Return a bounded page and its one-based range metadata."""
    if page is None and page_size is None:
        return list(rows), None
    size=page_size or 20
    if size<1: raise ValueError("--page-size must be at least 1")
    pages=max(1,math.ceil(len(rows)/size)); number=page or 1
    if number<1 or number>pages: raise ValueError(f"--page must be between 1 and {pages} for {len(rows)} cuts at {size} per page")
    first=(number-1)*size; shown=list(rows[first:first+size])
    return shown,{"page":number,"pages":pages,"page_size":size,"first":first+1,"last":first+len(shown),"total":len(rows)}

def paging_hint(table,rows,shown,info):
    text=f"showing cuts {info['first']}–{info['last']} of {info['total']}"
    if shown:
        shots=list(table.get("shots") or [])
        ordinal=next((i+1 for i,item in enumerate(shots) if item.get("shot_id")==shown[0].get("shot_id")),1)
        if info["page"]<info["pages"]:text+=f" · next: --page {info['page']+1}"
        text+=f" · or --all · or --shot {ordinal} · or --range {shown[0]['start']:.2f}..{shown[-1]['end']:.2f}"
    return text

def cut_table_payload(bundle,table):
    from astrid.sdk.timeline_checkout import Checkout
    rows=list(table.get("rows") or [])
    words=[]
    for word in Checkout(copy.deepcopy(dict(bundle))).words():
        clip=getattr(word,"clip",None)
        words.append({"id":word.id,"text":word.text,"start":word.start,"end":word.end,
                      "segment":word.segment,"clip_id":getattr(clip,"id",getattr(word,"clip_id",None)),
                      "shot_id":getattr(word,"shot_id",None)})
    clips=count_clips(bundle)
    payload={"fps":table.get("fps"),"duration":table.get("duration"),"complete":True,
        "counts":{"shots":len(table.get("shots") or []),"cuts":len(rows),"layers":sum(len(r.get("layers") or []) for r in rows),"words":len(words),"clips":clips},
        "shots":list(table.get("shots") or []),"rows":rows,"word_sources":dict(table.get("word_sources") or {}),"words":words}
    return decimal_seconds(payload)


_TIME_KEY=re.compile(r"(^|_)(start|end|duration|at|from|to|offset|hold|seconds)(_|$)")

def decimal_seconds(value):
    """Render rational time fields as decimals while retaining their exact form."""
    if isinstance(value,Mapping):
        out={}
        for key,child in value.items():
            name=str(key)
            rational=_is_rational(child)
            if _TIME_KEY.search(name) and rational:
                n,d=(child.get("numerator"),child.get("denominator",1)) if isinstance(child,Mapping) else child
                out[name]=round(_num(n)/(_num(d,1.0) or 1.0),6);out[f"{name}_exact"]=child
            elif _TIME_KEY.search(name) and isinstance(child,float):out[name]=round(child,6)
            else:out[name]=decimal_seconds(child)
        return out
    if isinstance(value,list):return [decimal_seconds(item) for item in value]
    return value


from astrid.core.timeline import clip_fields as cf  # noqa: E402  (the clip-level look fields)
from astrid.core.timeline.cuts import clip_duration  # noqa: E402  (one definition, re-exported)


# ----------------------------------------------------- clip-level look fields

AUDIO_LOOK_KEYS = frozenset(
    spec.stored_at_audio.removeprefix("params.")
    for spec in cf.FIELDS.values()
    if spec.stored_at_audio and spec.stored_at_audio.startswith("params.")
) | frozenset({"fadeInCurve", "fadeOutCurve"})  # audio fades are stored in params but are clip-level look fields


def _dotted(data: Mapping[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _look_path(spec: Any, audio: bool) -> str:
    return spec.stored_at_audio if audio and spec.stored_at_audio else spec.stored_at


def look_of(clip: Mapping[str, Any], *, audio: bool = False) -> dict[str, Any]:
    """Return the stored clip-level look fields declared by clip_fields.yaml."""
    paths = {spec.stored_at for spec in cf.FIELDS.values()}
    if audio:
        paths |= {spec.stored_at_audio for spec in cf.FIELDS.values() if spec.stored_at_audio}
    out: dict[str, Any] = {}
    for path in sorted(paths):
        value = _dotted(clip, path)
        if value is not None:
            out[path] = value
    return out


def _shown(spec: Any, value: Any, fps: float) -> str:
    text = None if value is None else spec.from_stored(value, fps=fps)
    if text is not None:
        return text
    if spec.default is None:
        return "none"
    if spec.kind == "gain":
        return f"{20 * math.log10(float(spec.default)):.1f}dB"
    return f"{float(spec.default):g}{spec.suffix or ''}"


def look_lines(
    before: Mapping[str, Any], after: Mapping[str, Any], *, audio: bool = False, fps: float = 30.0
) -> list[str]:
    """Describe changed clip-level fields in plain words."""
    a, b = look_of(before, audio=audio), look_of(after, audio=audio)
    changed = {path for path in set(a) | set(b) if a.get(path) != b.get(path)}
    lines: list[str] = []
    for name, spec in cf.FIELDS.items():
        if spec.stored_at_audio and not audio:
            continue
        path = _look_path(spec, audio)
        if path not in changed:
            continue
        was, now = a.get(path), b.get(path)
        was_text, now_text = _shown(spec, was, fps), _shown(spec, now, fps)
        if spec.default is None and was is None:
            lines.append(f"{name} {now_text} (was none)")
        elif spec.default is None and now is None:
            lines.append(f"{name} none (was {was_text})")
        else:
            lines.append(f"{name} {was_text} → {now_text}")
    return lines


def timecode(seconds: float, fps: float) -> str:
    """``HH:MM:SS:FF`` for timeline seconds (frames rounded to nearest)."""
    rate = max(1, int(round(fps)))
    frames = int(round(max(0.0, seconds) * fps))
    ff = frames % rate
    total = frames // rate
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}:{ff:02d}"


def _clock(seconds: float) -> str:
    total = max(0.0, seconds)
    return f"{int(total // 60)}:{total % 60:05.2f}"


def _fps(bundle: Mapping[str, Any]) -> float:
    for parent in (_map(bundle.get("parent")), _map(bundle.get("base_parent_payload"))):
        payload = _map(parent.get("payload"))
        configs = (
            _map(parent.get("config")),
            _map(payload.get("config")),
            parent,
        )
        for config in configs:
            canvas = _map(_map(_map(config.get("theme_overrides")).get("visual")).get("canvas"))
            fps = _num(canvas.get("fps"), 0.0)
            if fps > 0:
                return fps
    return 30.0


def _shot_name(shot: Mapping[str, Any], shot_id: str) -> str:
    payload = _map(shot.get("payload")) or _map(shot.get("base_payload"))
    metadata = _map(payload.get("metadata"))
    return str(payload.get("name") or metadata.get("name") or shot_id)


def _internal(shot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(shot.get("internal_timeline")) or _map(shot.get("base_internal_timeline"))


def _track_kinds(internal: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(track.get("id")): str(track.get("kind") or "").lower()
        for track in _list(internal.get("tracks"))
        if isinstance(track, Mapping) and track.get("id") is not None
    }


def _is_audio(clip: Mapping[str, Any], kinds: Mapping[str, str]) -> bool:
    track = str(clip.get("track") or "")
    return kinds.get(track) in AUDIO_KINDS or track in AUDIO_KINDS


def _clip_text(clip: Mapping[str, Any], limit: int = 36) -> str | None:
    params = _map(clip.get("params"))
    for key in TEXT_PARAM_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= limit else text[: limit - 1] + "…"
    chip = _map(params.get("chip")).get("text")
    if isinstance(chip, str) and chip.strip():
        return chip.strip()[:limit]
    return None


def _word_entry(item: Any) -> tuple[float, float, str | None] | None:
    if isinstance(item, Mapping):
        start, end = item.get("start", item.get("s")), item.get("end", item.get("e"))
        text = item.get("text", item.get("word"))
    elif isinstance(item, (list, tuple)) and len(item) >= 2:
        start, end = item[0], item[1]
        text = item[2] if len(item) > 2 else None
    else:
        return None
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end < start:
        return None
    return float(start), float(end), (str(text) if isinstance(text, str) and text.strip() else None)


def _narration_tokens(shot: Mapping[str, Any]) -> list[str]:
    payload = _map(shot.get("payload")) or _map(shot.get("base_payload"))
    tokens: list[str] = []
    for binding in _list(payload.get("text_bindings")):
        binding = _map(binding)
        if binding.get("kind") not in (None, "voiceover_script"):
            continue
        text = binding.get("text")
        if isinstance(text, str):
            tokens.extend(text.split())
    return tokens


# ------------------------------------------------------------------- model

def _picture_bed(clips: Sequence[Mapping[str, Any]], kinds: Mapping[str, str], track_order: Sequence[str]) -> str | None:
    """Compatibility wrapper: the picture bed of shot-relative clips (see ``astrid.core.timeline.cuts``)."""
    from astrid.core.timeline.cuts import _span, picture_bed

    return picture_bed([_span(clip, _num(clip.get("at")), kinds) for clip in clips], track_order)


def _words_for_occurrence(
    shot: Mapping[str, Any], start: float, clips: Sequence[Mapping[str, Any]], kinds: Mapping[str, str]
) -> tuple[list[tuple[float, float, str | None]], str]:
    from astrid.core.timeline.astrid_ns import read_app

    voice = sorted(
        (clip for clip in clips if _is_audio(clip, kinds) and clip_duration(clip) > 0),
        key=lambda clip: _num(clip.get("at")),
    )
    if not voice:
        return [], "no VO clip"
    words: list[tuple[float, float, str | None]] = []
    for clip in voice:
        offset = start + _num(clip.get("at"))
        for item in _list(read_app(clip).get("words")):
            entry = _word_entry(item)
            if entry is not None:
                words.append((offset + entry[0], offset + entry[1], entry[2]))
    if not words:
        return [], "VO clips carry no app.words"
    if all(text is not None for _s, _e, text in words):
        return words, "app.words"
    tokens = _narration_tokens(shot)
    if tokens and len(tokens) == len(words) and all(text is None for _s, _e, text in words):
        return [(s, e, token) for (s, e, _t), token in zip(words, tokens)], "app.words timing + narration text"
    reason = (
        f"app.words has timing only ({len(words)} words) and the narration has {len(tokens)}"
        if tokens else "app.words has timing only and the shot has no narration text"
    )
    return words, reason


def build_cut_table(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``{fps, duration, shots, rows, word_sources}`` for one bundle.

    Rows are :func:`astrid.core.timeline.cuts.picture_cuts`, the one cut
    definition ``visualize``, ``lint`` and ``editorial.pacing`` also use.
    """
    from astrid.core.timeline.cuts import occurrences_from_bundle
    from astrid.core.timeline.cut_index import cut_index

    fps = _fps(bundle)
    occurrences = occurrences_from_bundle(bundle)
    chapters: list[dict[str, Any]] = []
    word_sources: dict[str, str] = {}
    words_by_occurrence: dict[str, list[tuple[float, float, str | None]]] = {}
    voice_by_occurrence: dict[str, list[dict[str, Any]]] = {}
    end_of_timeline = 0.0
    for occurrence in occurrences:
        occurrence_id = occurrence["occurrence_id"]
        start, end = occurrence["start"], occurrence["end"]
        end_of_timeline = max(end_of_timeline, end)
        chapters.append({
            "shot_id": occurrence["shot_id"], "occurrence_id": occurrence_id,
            "name": occurrence["name"], "start": start, "end": end,
        })
        shot = occurrence["shot"]
        internal = _internal(shot)
        kinds = _track_kinds(internal)
        clips = [_map(clip) for clip in _list(internal.get("clips")) if isinstance(clip, Mapping)]
        words, source = _words_for_occurrence(shot, start, clips, kinds)
        word_sources[occurrence_id] = source
        words_by_occurrence[occurrence_id] = words
        voice_by_occurrence[occurrence_id] = [s for s in occurrence["spans"] if s["audio"] and s["end"] > s["start"]]
    rows: list[dict[str, Any]] = []
    for cut in cut_index(occurrences, fps=fps).rows:
        win_start, win_end = cut["start"], cut["end"]
        base = cut["clip"]
        layers = [
            {
                "clip_id": span["id"],
                "track": span["track"],
                "type": span["type"],
                "asset": span["clip"].get("asset") if isinstance(span["clip"].get("asset"), str) else None,
                "text": _clip_text(span["clip"]),
                "enters": round(span["start"] - win_start, 3) if span["start"] > win_start + 1.0 / fps else None,
            }
            for span in cut["layers"]
        ]
        words = words_by_occurrence.get(cut["occurrence_id"], [])
        said = [(s, e, text) for s, e, text in words if win_start - _EPS <= (s + e) / 2 < win_end - _EPS]
        vo_ids = [
            span["id"] for span in voice_by_occurrence.get(cut["occurrence_id"], [])
            if span["start"] < win_end and span["end"] > win_start
        ]
        text = " ".join(t for _s, _e, t in said if t) if said and all(t for _s, _e, t in said) else None
        rows.append({
            "index": cut["index"],
            "cut_id": cut.get("cut_id"),
            "intersections": dict(start=cut["start"], end=cut["end"],
                                  visual=[s["id"] for s in cut["layers"]],
                                  audio=[s["id"] for s in cut["audio"]]),
            "start": cut["start"],
            "end": cut["end"],
            "duration": cut["duration"],
            "tc_in": timecode(win_start, fps),
            "tc_out": timecode(win_end, fps),
            "shot": cut["shot"],
            "shot_id": cut["shot_id"],
            "occurrence_id": cut["occurrence_id"],
            "clip_id": cut["clip_id"],
            "track": cut["track"],
            "type": cut["type"],
            "asset": base.get("asset") if base and isinstance(base.get("asset"), str) else None,
            "text": _clip_text(base) if base else None,
            "layers": layers,
            "words": len(said),
            "say": text,
            "vo": vo_ids,
            "deliberate_hold": cut["deliberate_hold"],
            "sequence": cut.get("sequence"),
        })
    labelled = _chapter_labels(bundle, rows, end_of_timeline)
    return {
        "fps": fps,
        "duration": round(end_of_timeline, 6),
        "shots": labelled or chapters,
        "rows": rows,
        "word_sources": word_sources,
    }


def _chapter_labels(bundle: Mapping[str, Any], rows: list[dict[str, Any]], end: float) -> list[dict[str, Any]]:
    """Chapters as labels (``parent.config.chapters``: each starts at a cut id) instead of shots.

    Renames each row's ``shot`` to its chapter and returns the chapter list; empty when the
    timeline has no labels (then chapters are its shots, as before)."""
    table = _list(film_intent(_map(_map(bundle.get("parent")).get("config"))).get("chapters"))
    starts = {str(_map(row).get("from")): str(_map(row).get("name")) for row in table if _map(row).get("from")}
    if not starts:
        return []
    out: list[dict[str, Any]] = []
    current = None
    for row in rows:
        cut_id = None
        base = _cut_clip(bundle, row)
        if base is not None:
            cut_id = read_app(base).get("cut")
        if cut_id in starts:
            current = starts[cut_id]
            if out:
                out[-1]["end"] = row["start"]
            out.append({"shot_id": row["shot_id"], "occurrence_id": row["occurrence_id"], "name": current,
                        "start": row["start"], "end": end})
        if current is not None:
            row["shot"] = current
    return out


def _cut_clip(bundle: Mapping[str, Any], row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    shot = _map(_map(bundle.get("shots")).get(str(row.get("shot_id"))))
    for clip in _list(_map(shot.get("internal_timeline")).get("clips")):
        if isinstance(clip, Mapping) and clip.get("id") == row.get("clip_id"):
            return clip
    return None


def _parse_range(value: str) -> tuple[float, float]:
    first, separator, second = str(value).partition("..")
    if not separator:
        raise ValueError("range must be START..END seconds")

    def seconds(text: str) -> float:
        text = text.strip()
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return float(numerator) / float(denominator)
        if ":" in text:
            minutes, rest = text.split(":", 1)
            return float(minutes) * 60 + float(rest)
        return float(text)

    low, high = seconds(first), seconds(second)
    if high <= low:
        raise ValueError("range END must be greater than START")
    return low, high


def filter_rows(table: Mapping[str, Any], *, range_value: str | None = None, shot: str | None = None) -> list[dict[str, Any]]:
    """Rows overlapping ``START..END`` seconds and/or in one shot (id, name or 1-based ordinal)."""
    rows = list(table.get("rows") or [])
    if range_value:
        low, high = _parse_range(range_value)
        if high - low < 1e-3:
            high = low + 1e-3
        rows = [row for row in rows if row["end"] > low + _EPS and row["start"] < high - _EPS]
    if shot:
        ids = resolve_shot(table, shot)
        rows = [row for row in rows if row["shot_id"] in ids]
    return rows


# ---------------------------------------------------------------- printing

def render_summary(
    table: Mapping[str, Any], *, title: str = "", words_chars: int = 60
) -> str:
    """One line per shot (chapter): number, name, time span, cut count and the first words."""
    fps = float(table.get("fps") or 30)
    rows = list(table.get("rows") or [])
    lines: list[str] = []
    if title:
        lines.append(title)
    duration = float(table.get("duration") or 0)
    lines.append(
        f"{_clock(duration)} ({timecode(duration, fps)}) at {fps:g} fps · {len(table.get('shots') or [])} chapters · "
        f"{len(rows)} cuts"
    )
    for number, shot in enumerate(table.get("shots") or [], start=1):
        # a chapter is its rows in time (a label over cuts, or a shot's occurrence)
        members = [row for row in rows if shot["start"] - 1e-6 <= row["start"] < shot["end"] - 1e-6
                   and row["occurrence_id"] == shot["occurrence_id"]]
        said = next((row["say"] for row in members if row.get("say")), "")
        first = (said[:words_chars] + "…") if len(said) > words_chars else said
        lines.append(
            f"{number:>3}  {shot['name']}  {shot['start']:6.2f}–{shot['end']:<6.2f} s  "
            f"{len(members):>3} cuts  " + (f'"{first}"' if first else "(no words)")
        )
    return "\n".join(lines)


def _layer_label(layer: Mapping[str, Any]) -> str:
    label = layer["type"]
    if layer.get("asset"):
        label += f" {layer['asset']}"
    if layer.get("text"):
        label += f' "{layer["text"]}"'
    if layer.get("enters") is not None:
        label += f" @+{layer['enters']:.2f}"
    return label


def _row_lines(
    row: Mapping[str, Any], fps: float, width: int, changed_clip_ids: set[str] | None = None,
) -> list[str]:
    parts = []
    if row["type"] and row["type"] not in (row.get("clip_id") or ""):
        parts.append(row["type"])
    if row.get("asset"):
        parts.append(str(row["asset"]))
    if row.get("text"):
        parts.append(f'"{row["text"]}"')
    sequence = row.get("sequence") or {}
    if sequence:
        parts.append(f"sequence ×{sequence.get('steps')} steps")
    changed = bool(changed_clip_ids and (
        str(row.get("clip_id") or "") in changed_clip_ids
        or any(str(layer.get("clip_id") or "") in changed_clip_ids
               for layer in row.get("layers") or [] if isinstance(layer, Mapping))
    ))
    head = (
        f"{'✎' if changed else ' '} {row['index']:>3}  {row['start']:6.2f}–{row['end']:<6.2f} {row['duration']:5.2f}s  "
        f"{row['tc_in']}  {row.get('clip_id') or '(no picture clip)'}"
        + (f"  {' '.join(parts)}" if parts else "")
    )
    lines = [head]
    if row["layers"]:
        counts: dict[str, int] = {}
        for layer in row["layers"]:
            counts[layer["type"]] = counts.get(layer["type"], 0) + 1
        labels: list[str] = []
        grouped: set[str] = set()
        for layer in row["layers"]:
            kind = layer["type"]
            if counts[kind] >= 3:
                if kind not in grouped:
                    grouped.add(kind)
                    labels.append(f"{kind} ×{counts[kind]}")
                continue
            labels.append(_layer_label(layer))
        layers = " · ".join(labels)
        lines.append(_wrap("      + " + layers, width))
    if row.get("say"):
        lines.append(_wrap(f'      VO "{row["say"]}"', width))
    elif row.get("words"):
        lines.append(f"      VO {row['words']} words ({', '.join(row['vo'])}; no word text)")
    elif row.get("vo"):
        lines.append(f"      VO (pause) {', '.join(row['vo'])}")
    return lines


def _wrap(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render_cut_table(
    table: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]] | None = None,
    *,
    title: str = "",
    width: int = 160,
    changed_clip_ids: set[str] | None = None,
) -> str:
    """Human cut table: a header, then rows grouped under their shot."""
    fps = float(table.get("fps") or 30)
    rows = list(table.get("rows") or []) if rows is None else list(rows)
    shots = list(table.get("shots") or [])
    duration = float(table.get("duration") or 0)
    lines: list[str] = []
    if title:
        lines.append(title)
    lines.append(
        f"{_clock(duration)} ({timecode(duration, fps)}) at {fps:g} fps · {len(shots)} shots · "
        f"{len(table.get('rows') or [])} cuts" + (f" · showing {len(rows)}" if len(rows) != len(table.get("rows") or []) else "")
    )
    lines.append(
        "Times are timeline seconds (what --range/--at take) and HH:MM:SS:FF timecode. A cut is one clip on the "
        "shot's picture-bed track; '+' lists layers over it (@+s = enters later)."
    )
    by_shot: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_shot.setdefault(row["occurrence_id"], []).append(row)
    sources = _map(table.get("word_sources"))
    for shot in shots:
        members = by_shot.get(shot["occurrence_id"])
        if not members:
            continue
        lines.append("")
        lines.append(
            f"{shot['name']} · {shot['start']:.2f}–{shot['end']:.2f} s ({shot['end'] - shot['start']:.2f} s) · "
            f"shot {shot['shot_id']} · words: {sources.get(shot['occurrence_id'], 'unknown')}"
        )
        for row in members:
            lines.extend(_row_lines(row, fps, width, changed_clip_ids))
    if not rows:
        lines.append("(no cuts in this selection)")
    return "\n".join(lines)


# -------------------------------------------------------------------- diff

def base_bundle(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The pinned 'before' of a checkout file, as a bundle ``build_cut_table`` reads."""
    before = copy.deepcopy(dict(candidate))
    mapped_placements = _map(candidate.get("source_mapping")).get("placements")
    if isinstance(mapped_placements, Mapping) and mapped_placements:
        base_placements = list(mapped_placements.values())
    elif isinstance(mapped_placements, list) and mapped_placements:
        base_placements = mapped_placements
    else:
        base_placements = list(_map(candidate.get("base_placements")).values())
    before["placements"] = [copy.deepcopy(dict(row)) for row in base_placements if isinstance(row, Mapping)]
    if not before["placements"]:
        # A freshly re-read published head can omit old base-layout metadata:
        # in that case its current layout is the base, not an empty timeline.
        before["placements"] = copy.deepcopy(list(candidate.get("placements") or []))
    if isinstance(candidate.get("base_parent_payload"), Mapping):
        before["parent"] = copy.deepcopy(dict(candidate["base_parent_payload"]))
    shots: dict[str, Any] = {}
    placed = {str(row.get("shot_id")) for row in before["placements"]}
    for shot_id, shot in _map(candidate.get("shots")).items():
        if shot_id not in placed:
            continue
        shot = dict(shot)
        shots[shot_id] = {
            "shot_id": shot_id,
            "payload": copy.deepcopy(shot.get("base_payload") or shot.get("payload")),
            "internal_timeline": copy.deepcopy(shot.get("base_internal_timeline") or shot.get("internal_timeline")),
        }
    before["shots"] = shots
    return before


def _clip_index(bundle: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    from astrid.core.timeline.astrid_ns import read_app

    index: dict[tuple[str, str], dict[str, Any]] = {}
    shots = _map(bundle.get("shots"))
    for placement in _list(bundle.get("placements")):
        placement = _map(placement)
        shot_id = str(placement.get("shot_id") or "")
        occurrence_id = str(placement.get("occurrence_id") or shot_id)
        start = _num(placement.get("at_ms", _map(placement.get("placement")).get("start_ms"))) / 1000.0
        shot = _map(shots.get(shot_id))
        internal = _internal(shot)
        kinds = _track_kinds(internal)
        for clip in _list(internal.get("clips")):
            clip = _map(clip)
            clip_id = str(clip.get("id") or "")
            if not clip_id:
                continue
            clip_start = start + _num(clip.get("at"))
            audio = _is_audio(clip, kinds)
            params = clip.get("params")
            if audio and isinstance(params, Mapping) and any(k in AUDIO_LOOK_KEYS for k in params):
                # Audio fades live in params for historical storage reasons,
                # but are clip-level look fields in the editing contract.
                params = {k: v for k, v in params.items() if k not in AUDIO_LOOK_KEYS} or None
            look_roots = {
                path.split(".", 1)[0]
                for spec in cf.FIELDS.values()
                for path in (spec.stored_at, spec.stored_at_audio)
                if path
            }
            index[(occurrence_id, clip_id)] = {
                "shot": _shot_name(shot, shot_id),
                "occurrence_id": occurrence_id,
                "clip_id": clip_id,
                "track": str(clip.get("track") or ""),
                "audio": audio,
                "start": clip_start,
                "end": clip_start + clip_duration(clip),
                "asset": clip.get("asset"),
                "params": json.dumps(params, sort_keys=True, default=str),
                "look": json.dumps(look_of(clip, audio=audio), sort_keys=True, default=str),
                "app": json.dumps(dict(read_app(clip)), sort_keys=True, default=str),
                "extras": {
                    key: copy.deepcopy(value) for key, value in clip.items()
                    if key not in {
                        "id", "track", "asset", "at", "hold", "from", "to", "params", "app",
                        *look_roots,
                    }
                },
            }
    return index


def _placement_index(bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Index placed shots by their stable occurrence identity."""
    result: dict[str, dict[str, Any]] = {}
    for raw in _list(bundle.get("placements")):
        if not isinstance(raw, Mapping):
            continue
        occurrence_id = raw.get("occurrence_id") or raw.get("id")
        if occurrence_id is not None and str(occurrence_id):
            result[str(occurrence_id)] = dict(raw)
    return result


def _values_equal(before: Any, after: Any, tolerance: float) -> bool:
    """Compare authored values while ignoring bounded floating-point noise."""
    if isinstance(before, bool) or isinstance(after, bool):
        return before == after
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return abs(float(before) - float(after)) <= tolerance
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        return before.keys() == after.keys() and all(
            _values_equal(before[key], after[key], tolerance) for key in before
        )
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        return len(before) == len(after) and all(
            _values_equal(left, right, tolerance) for left, right in zip(before, after)
        )
    return before == after


def _serialized_values_equal(before: str, after: str, tolerance: float) -> bool:
    try:
        return _values_equal(json.loads(before), json.loads(after), tolerance)
    except (TypeError, ValueError):
        return before == after


def _placement_changes(
    before: Mapping[str, Any], after: Mapping[str, Any], *, tolerance: float,
) -> list[dict[str, Any]]:
    old, new = _placement_index(before), _placement_index(after)
    changes: list[dict[str, Any]] = []
    for occurrence_id in sorted(old.keys() | new.keys()):
        previous, current = old.get(occurrence_id), new.get(occurrence_id)
        if previous is None or current is None:
            changes.append({
                "kind": "added" if previous is None else "removed",
                "occurrence_id": occurrence_id,
                "before": copy.deepcopy(previous),
                "after": copy.deepcopy(current),
                "fields": [],
            })
            continue
        fields = sorted(
            key for key in previous.keys() | current.keys()
            if key not in previous
            or key not in current
            or not _values_equal(previous[key], current[key], tolerance)
        )
        if fields:
            changes.append({
                "kind": "changed",
                "occurrence_id": occurrence_id,
                "before": copy.deepcopy(previous),
                "after": copy.deepcopy(current),
                "fields": fields,
            })
    return changes


def diff_bundles(before: Mapping[str, Any], after: Mapping[str, Any], *, tolerance: float = 1e-4) -> dict[str, Any]:
    """Clip-level and cut-level differences between two bundles, in timeline seconds."""
    # A legacy flat app intent and its app.astrid representation describe the
    # same authored state. Normalize detached copies of both sides so the
    # storage migration is not reported as an edit.
    before_copy, after_copy = copy.deepcopy(dict(before)), copy.deepcopy(dict(after))
    from astrid.sdk import timeline_intent
    timeline_intent.migrate(before_copy)
    timeline_intent.migrate(after_copy)
    old, new = _clip_index(before_copy), _clip_index(after_copy)
    changes: list[dict[str, Any]] = []
    for key in sorted(set(old) | set(new), key=lambda k: (min(old.get(k, new.get(k))["start"], new.get(k, old.get(k))["start"]), k)):
        a, b = old.get(key), new.get(key)
        if a is None or b is None:
            row = b or a
            changes.append({
                "kind": "added" if a is None else "removed",
                "shot": row["shot"], "clip_id": row["clip_id"], "track": row["track"],
                "before": None if a is None else [round(a["start"], 6), round(a["end"], 6)],
                "after": None if b is None else [round(b["start"], 6), round(b["end"], 6)],
                "fields": [],
            })
            continue
        fields = []
        if abs(a["start"] - b["start"]) > tolerance:
            fields.append("start")
        if abs(a["end"] - b["end"]) > tolerance:
            fields.append("end")
        for name in ("track", "asset", "params"):
            if name == "params":
                changed = not _serialized_values_equal(a[name], b[name], tolerance)
            else:
                changed = a[name] != b[name]
            if changed:
                fields.append(name)
        if not _serialized_values_equal(a["look"], b["look"], tolerance):
            fields.append("look")
        notes = {"why", "why_layers", "deliberate_hold", "deliberate"}
        app_a, app_b = (
            json.loads(value) if isinstance(value, str) else dict(value or {})
            for value in (a["app"], b["app"])
        )
        app_a, app_b = dict(app_a or {}), dict(app_b or {})
        if {key: value for key, value in app_a.items() if key not in notes} != {
            key: value for key, value in app_b.items() if key not in notes
        }:
            fields.append("app")
        if any(app_a.get(key) != app_b.get(key) for key in notes):
            fields.append("note")
        before_extra, after_extra = a["extras"], b["extras"]
        for name in sorted(before_extra.keys() | after_extra.keys()):
            if not _values_equal(before_extra.get(name), after_extra.get(name), tolerance):
                fields.append(name)
        if fields:
            changes.append({
                "kind": "look" if fields == ["look"] else "changed",
                "shot": b["shot"], "clip_id": b["clip_id"], "track": b["track"],
                "before": [round(a["start"], 6), round(a["end"], 6)],
                "after": [round(b["start"], 6), round(b["end"], 6)],
                "fields": fields,
            })
    old_table, new_table = build_cut_table(before_copy), build_cut_table(after_copy)
    old_points = sorted({round(row["start"], 4) for row in old_table["rows"]} - {0.0})
    new_points = sorted({round(row["start"], 4) for row in new_table["rows"]} - {0.0})
    removed = [p for p in old_points if not any(abs(p - q) <= tolerance for q in new_points)]
    added = [p for p in new_points if not any(abs(p - q) <= tolerance for q in old_points)]
    moved = []
    if len(removed) == len(added):
        moved = [{"from": a, "to": b, "delta": round(b - a, 4)} for a, b in zip(removed, added)]
        removed, added = [], []
    return_windows = _change_windows(changes)
    return {
        "changes": changes,
        "placement_changes": _placement_changes(before_copy, after_copy, tolerance=tolerance),
        "cut_points": {"moved": moved, "added": added, "removed": removed},
        "cuts_before": len(old_table["rows"]),
        "cuts_after": len(new_table["rows"]),
        "duration_before": old_table["duration"],
        "duration_after": new_table["duration"],
        "windows": return_windows,
        "fps": new_table["fps"],
    }


def _change_windows(changes: Sequence[Mapping[str, Any]], *, pad: float = 0.5, join: float = 1.5, limit: int = 4) -> list[list[float]]:
    """Short windows around the times that actually changed (edit points), merged when close."""
    points: list[float] = []
    for change in changes:
        before, after = change.get("before"), change.get("after")
        fields = set(change.get("fields") or [])
        if before is None or after is None:
            points.extend(before or after or [])
            continue
        if "start" in fields:
            points.extend([before[0], after[0]])
        if "end" in fields:
            points.extend([before[1], after[1]])
        if fields - {"start", "end"}:
            points.append((after[0] + after[1]) / 2)
    windows: list[list[float]] = []
    for point in sorted(points):
        if windows and point - windows[-1][1] <= join:
            windows[-1][1] = point
        else:
            windows.append([point, point])
    return [[round(max(0.0, low - pad), 3), round(high + pad, 3)] for low, high in windows[:limit]]


def render_diff(diff: Mapping[str, Any], *, title: str = "", limit: int | None = 40) -> str:
    changes = list(diff.get("changes") or [])
    placement_changes = list(diff.get("placement_changes") or [])
    points = _map(diff.get("cut_points"))
    kinds: dict[str, int] = {}
    for change in changes:
        kinds[change["kind"]] = kinds.get(change["kind"], 0) + 1
    lines = [title] if title else []
    if not changes and not placement_changes:
        lines.append("No clip changes (times, tracks, assets, params and app data are identical).")
        return "\n".join(lines)
    shots = sorted({change["shot"] for change in changes})
    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    clip_summary = f" ({summary})" if summary else ""
    placement_summary = (
        f"; {len(placement_changes)} placement change"
        f"{'s' if len(placement_changes) != 1 else ''}"
        if placement_changes else ""
    )
    lines.append(
        f"{len(changes)} clip changes{clip_summary} in {len(shots)} shot(s): {', '.join(shots)}"
        f"{placement_summary} · "
        f"cuts {diff.get('cuts_before')} → {diff.get('cuts_after')} · "
        f"length {diff.get('duration_before'):.2f} → {diff.get('duration_after'):.2f} s"
    )
    for placement in placement_changes:
        fields = ", ".join(placement.get("fields") or []) or placement["kind"]
        lines.append(f"  placement {placement['occurrence_id']}: {fields}")
    for move in points.get("moved") or []:
        lines.append(f"  cut moved {move['from']:.2f} → {move['to']:.2f} s ({move['delta']:+.2f} s)")
    for point in points.get("added") or []:
        lines.append(f"  cut added at {point:.2f} s")
    for point in points.get("removed") or []:
        lines.append(f"  cut removed at {point:.2f} s")
    current = None
    for change in (changes if limit is None else changes[:limit]):
        if change["shot"] != current:
            current = change["shot"]
            lines.append(f"{current}")
        before = change.get("before")
        after = change.get("after")
        span = lambda pair: f"{pair[0]:.2f}–{pair[1]:.2f}" if pair else "—"  # noqa: E731
        detail = ""
        if before and after:
            detail = f"  dur {before[1] - before[0]:.2f} → {after[1] - after[0]:.2f}"
        extra = [field for field in change.get("fields") or [] if field not in ("start", "end")]
        lines.append(
            f"  {change['kind']:<8} {change['clip_id']:<28} {change['track']:<7} {span(before)} → {span(after)}{detail}"
            + (f"  ({', '.join(extra)} changed)" if extra else "")
        )
    if limit is not None and len(changes) > limit:
        lines.append(f"  … {len(changes) - limit} more (--all prints every one; --json for data)")
    return "\n".join(lines)
