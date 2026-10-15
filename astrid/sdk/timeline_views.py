"""Two more ways to read one timeline: as a word-anchored script and as code.

``timelines show`` prints the cut table by default (``timeline_cuts``). These
views answer the questions an editor asks next:

- **script** (``--as script``): the voiceover in time order, with every cut,
  layer entrance/exit and element keyframe placed between the words it lands
  on, and how far each cut sits from the nearest word onset. This is the view
  for timing decisions: "does the cut land on the word?", "what fires on
  'rebuilt'?", "is there a dead stretch?".
- **code** (``--as code``): the composition as a small readable program, one
  block per shot and cut, each element called like a function with its
  authored props, its keyframes resolved to timeline seconds, and the
  element's source file one hop away. This is the view for understanding and
  changing what each element does.

Both are pure text over an authoring bundle, so two revisions can be diffed
line by line (``timelines diff --as script|code``).

Keyframes: astrid_motion elements key internal motion by clip-relative
*frames* (``at``, ``appearAt``, ``atFrame``, ``startAt``; see each
element.yaml). They are resolved here as ``clip start + frame / fps``.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from astrid.core.foundation.paths import REPO_ROOT
from astrid.sdk.timeline_cuts import (
    _clip_text,
    _fps,
    _internal,
    _is_audio,
    _list,
    _map,
    _num,
    _shot_name,
    _track_kinds,
    _words_for_occurrence,
    build_cut_table,
    clip_duration,
    timecode,
)

__all__ = ["element_source", "clip_keyframes", "render_script", "render_code", "render_at", "render_clips"]

FRAME_KEYS = ("at", "appearAt", "atFrame", "startAt")
LABEL_KEYS = ("text", "label", "title", "name", "glyph", "author", "interface", "highlight")


@lru_cache(maxsize=1)
def _v3_element_registry():
    """Load the declared V3 element registry used by rendering."""
    from astrid.core.element.registry import load_default_registry

    try:
        return load_default_registry(project_root=REPO_ROOT)
    except Exception:
        # A dirty candidate can contain an unrelated optional pack whose
        # resources are not yet joined.  Keep this read-only label available
        # from the current rendering pack's V3 declarations without falling
        # back to any legacy directory scan.
        from astrid.core.pack.manifest import load_manifest_mapping

        pack_root = Path(REPO_ROOT) / "astrid" / "packs" / "rendering"
        pack = load_manifest_mapping(pack_root / "pack.yaml", manifest_kind="pack")
        elements = []
        for declared_id, item in (pack.get("rendering") or {}).items():
            if not isinstance(item, Mapping) or item.get("type") != "element":
                continue
            manifest_path = pack_root / str(item.get("path"))
            manifest = load_manifest_mapping(manifest_path, manifest_kind="element")
            element_root = manifest_path.parent
            component = element_root / "component.tsx"
            for resource in item.get("resources") or ():
                if isinstance(resource, Mapping) and resource.get("kind") == "implementation":
                    component = pack_root / str(resource.get("path"))
                    break
            elements.append(SimpleNamespace(
                id=str(manifest.get("id") or str(declared_id).rsplit("/", 1)[-1]),
                component=component.resolve(),
            ))
        return SimpleNamespace(list=lambda: tuple(elements))


@lru_cache(maxsize=None)
def element_source(clip_type: str) -> str | None:
    """Repository-relative V3 component path for a declared element, if available.

    The pack manifest and resolved ``ElementRegistry`` are authoritative.  In
    particular, this deliberately does not scan legacy ``packs/*/elements``
    directories, because a folder that is not declared by a V3 pack is not a
    renderable element.
    """
    if not clip_type or "/" in clip_type or clip_type == "media":
        return None
    try:
        definitions = _v3_element_registry().list()
    except Exception:
        # Source labels are optional presentation data; an unavailable
        # declaration corpus must not make script/code views unusable.
        return None
    for definition in definitions:
        if definition.id != clip_type:
            continue
        component = Path(definition.component).resolve()
        try:
            return str(component.relative_to(Path(REPO_ROOT).resolve()))
        except ValueError:
            return str(component)
    return None


def _item_label(item: Mapping[str, Any]) -> str:
    for key in LABEL_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= 28 else text[:27] + "…"
    lines = item.get("lines")
    if isinstance(lines, list) and lines and isinstance(lines[0], str):
        return lines[0][:27] + ("…" if len(lines[0]) > 27 else "")
    keys = [key for key in item if key not in FRAME_KEYS]
    return ",".join(keys[:3]) or "step"


def clip_keyframes(clip: Mapping[str, Any], clip_start: float, fps: float) -> list[tuple[float, str]]:
    """``(timeline seconds, 'path → label')`` for each frame-keyed item in a clip's params."""
    found: list[tuple[float, str]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            frame_key = next((key for key in FRAME_KEYS if isinstance(value.get(key), (int, float))
                              and not isinstance(value.get(key), bool)), None)
            if frame_key is not None and path:
                frame = float(value[frame_key])
                found.append((clip_start + frame / fps, f"{path} → {_item_label(value)} (f{int(frame)})"))
            for key, child in value.items():
                if isinstance(child, (Mapping, list)):
                    walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, Mapping):
                    walk(child, f"{path}[{index}]")

    walk(_map(clip.get("params")), "")
    return sorted(found)


def _placements(bundle: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = [_map(item) for item in _list(bundle.get("placements")) if isinstance(item, Mapping)]
    return sorted(rows, key=lambda row: _num(row.get("at_ms", _map(row.get("placement")).get("start_ms"))))


def _in_window(start: float, end: float, window: Sequence[float] | None) -> bool:
    return window is None or (end > window[0] and start < window[1])


def _shot_context(bundle, placement):
    shot_id=str(placement.get("shot_id") or "")
    shots=_map(bundle.get("shots")); shot=_map(shots.get(shot_id))
    start=_num(placement.get("at_ms",_map(placement.get("placement")).get("start_ms")))/1000
    end=start+_num(placement.get("duration_ms"))/1000
    return shot,shot_id,start,end


_EPS=1e-6

def render_at(bundle,table,seconds,*,changed=None,show_command=""):
    """Show the clips and spoken word visible at one timeline moment."""
    from astrid.sdk.timeline_checkout import Checkout
    rows=list(table.get("rows") or []); fps=float(table.get("fps") or 30)
    index=next((i for i,row in enumerate(rows) if row["start"]-_EPS<=seconds<row["end"]-_EPS),None)
    if index is None:return None
    row=rows[index]; lines=[f"at {seconds:.2f} s ({timecode(seconds,fps)}) · cut {row['index']} of {len(rows)} · {row['start']:.2f}–{row['end']:.2f} s · {row['shot']}","on screen:"]
    placement=next((p for p in _placements(bundle) if str(p.get("occurrence_id") or p.get("shot_id"))==row["occurrence_id"]),None)
    visible=[]
    if placement:
        shot,_sid,start,_end=_shot_context(bundle,placement); internal=_internal(shot); kinds=_track_kinds(internal)
        for clip in _list(internal.get("clips")):
            if not isinstance(clip,Mapping) or _is_audio(clip,kinds):continue
            duration=clip_duration(clip)
            if duration<=0:continue
            begin=start+_num(clip.get("at")); end=begin+duration
            if begin-_EPS<=seconds<end-_EPS:
                what=clip.get("asset") if isinstance(clip.get("asset"),str) else (_clip_text(clip,40) or "-")
                mark="✎ " if changed and str(clip.get("id")) in changed else "  "
                visible.append((begin,f"  {mark}{str(clip.get('id')):<30} {str(clip.get('clipType')):<18} {str(clip.get('track')):<10} {str(what):<28} {begin:7.2f}–{end:7.2f} s"))
    lines.extend(line for _,line in sorted(visible))
    if not visible:lines.append("  (nothing visible)")
    words=Checkout(dict(bundle)).words(); spoken=[w for w in words if w.start-_EPS<=seconds<w.end-_EPS]
    lines.append("being said:")
    lines.append(f"  ▶ {spoken[0].text or '·'} {spoken[0].start:.2f}–{spoken[0].end:.2f} s [{spoken[0].id}]" if spoken else "  (no word spoken here)")
    near=[w for w in words if abs(w.start-seconds)<=1]
    if near:
        lines.append("words within ±1 s:")
        for w in near:lines.append(f"  {'▶' if spoken and w.id==spoken[0].id else ' '} {w.start:7.2f}  {w.text or '·'}  [{w.id}]")
    nav=[]
    if index:nav.append(f"--at {rows[index-1]['start']:.2f}")
    if index+1<len(rows):nav.append(f"--at {rows[index+1]['start']:.2f}")
    if nav:lines.append("next: "+" · ".join(nav)+(f"   ({show_command})" if show_command else ""))
    return "\n".join(lines)


def render_clips(bundle,shot_ids,*,changed=None):
    """List every authored clip in selected shots, including its timeline time."""
    lines=[]
    for placement in _placements(bundle):
        if str(placement.get("shot_id") or "") not in shot_ids:continue
        shot,sid,start,_end=_shot_context(bundle,placement); internal=_internal(shot); kinds=_track_kinds(internal)
        clips=[c for c in _list(internal.get("clips")) if isinstance(c,Mapping)]
        lines.append(f"{_shot_name(shot,sid)} · shot {sid} · {len(clips)} clips")
        for clip in sorted(clips,key=lambda c:(_num(c.get("at")),str(c.get("track")))):
            duration=clip_duration(clip); begin=start+_num(clip.get("at")); what=clip.get("asset") if isinstance(clip.get("asset"),str) else (_clip_text(clip,60) or "-")
            kind="audio" if _is_audio(clip,kinds) else "visual"; app=_map(clip.get("app")); astrid=_map(app.get("astrid")); anchor=_map({**app,**astrid}.get("anchor"))
            tail=f"  anchor {anchor.get('word')!r} {_num(anchor.get('offset_s')):+.2f}s" if anchor else ""
            mark="✎ " if changed and str(clip.get("id")) in changed else "  "
            lines.append(f"  {mark}{str(clip.get('id')):<30} {str(clip.get('clipType')):<18} {str(clip.get('track')):<10} {kind:<6} {str(what):<30} {begin:8.2f}–{begin+duration:8.2f} s{tail}")
    return "\n".join(lines) if lines else "(no clips in this selection)"


# ------------------------------------------------------------------ script

def render_asset_provenance(clip: Any) -> str | None:
    """Pixel provenance saved with a timeline asset, formatted for an address-specific show."""
    if getattr(clip, "element", None) != "am-sprite" or not getattr(clip, "asset", None):
        return None
    tl = getattr(clip, "_tl", None)
    if tl is None:
        return None
    internal = tl._internal(clip.shot_id)
    registry = _map(internal.get("registry"))
    assets = _map(registry.get("assets"))
    entry = _map(assets.get(clip.asset))
    provenance = _map(entry.get("pixel_provenance"))
    if not provenance:
        return None
    lattice = _map(provenance.get("lattice"))
    detail = []
    if provenance.get("executor"):
        detail.append(str(provenance["executor"]))
    if provenance.get("run_id"):
        detail.append(f"run {provenance['run_id']}")
    if entry.get("handle"):
        detail.append(str(entry["handle"]))
    if lattice:
        block = lattice.get("block") or lattice.get("drawn_block")
        detected = lattice.get("detected")
        lattice_text = f"lattice {float(block):g} px/block" if isinstance(block, (int, float)) else "lattice recorded"
        if detected is False:
            lattice_text = "lattice not detected"
        detail.append(lattice_text)
    integrity = provenance.get("outline_integrity")
    if isinstance(integrity, Mapping):
        integrity = integrity.get("fraction")
    if isinstance(integrity, (int, float)):
        detail.append(f"outline integrity {float(integrity):.2f}")
    return f"sprite provenance {clip.address}: " + " → ".join(detail) if detail else None


def _placement_span(placement: Mapping[str, Any]) -> tuple[float, float]:
    start = _num(placement.get("at_ms", _map(placement.get("placement")).get("start_ms"))) / 1000.0
    return start, start + _num(placement.get("duration_ms")) / 1000.0


def _id_range(words: Sequence[Any]) -> str:
    """Compact word addresses, preserving segment boundaries."""
    groups: list[list[Any]] = []
    for word in words:
        if groups and groups[-1][0] == word.segment:
            groups[-1][2] = word.index
        else:
            groups.append([word.segment, word.index, word.index])
    return " + ".join(
        f"{segment}:{first}" if first == last else f"{segment}:{first}–{last}"
        for segment, first, last in groups
    )


def _marked(text: str, changed: set[str] | None, *clip_ids: Any) -> str:
    return ("✎ " + text) if changed and any(str(clip_id) in changed for clip_id in clip_ids if clip_id) else text

def render_script(
    bundle: Mapping[str, Any],
    *,
    window: Sequence[float] | None = None,
    shot_ids: set[str] | None = None,
    pause: float = 0.3,
    width: int = 150,
    changed: set[str] | None = None,
) -> str:
    """Words in time order with cuts, layers, keyframes, and their edit addresses."""
    from astrid.sdk.timeline_checkout import Checkout

    fps = _fps(bundle)
    table = build_cut_table(bundle)
    cut_rows = {(row["occurrence_id"], round(row["start"], 4)): row for row in table["rows"]}
    shots = _map(bundle.get("shots"))
    # Checkout may migrate legacy flat app metadata in its private document;
    # give it a deep copy so a read-only view never rewrites its input bundle.
    all_words = Checkout(copy.deepcopy(dict(bundle))).words()
    lines = [
        "Script view: words in time order; ┃ cut, + enters, − exits, ◆ element keyframe (clip frame resolved to "
        f"timeline seconds at {fps:g} fps). 'Δ' is the cut's distance to the nearest word onset (− = before the word). "
        "[id] is the clip id; ✎ marks an unpublished edit.",
    ]
    for placement in _placements(bundle):
        shot_id = str(placement.get("shot_id") or "")
        if shot_ids and shot_id not in shot_ids:
            continue
        occurrence_id = str(placement.get("occurrence_id") or shot_id)
        start, end = _placement_span(placement)
        if not _in_window(start, end, window):
            continue
        shot = _map(shots.get(shot_id))
        internal = _internal(shot)
        kinds = _track_kinds(internal)
        clips = [_map(c) for c in _list(internal.get("clips")) if isinstance(c, Mapping)]
        words, source = _words_for_occurrence(shot, start, clips, kinds)
        onsets = [s for s, _e, _t in words]
        events: list[tuple[float, int, str]] = []  # (time, order, text)
        for row in table["rows"]:
            if row["occurrence_id"] != occurrence_id:
                continue
            delta = ""
            if onsets:
                nearest = min(onsets, key=lambda onset: abs(onset - row["start"]))
                gap = row["start"] - nearest
                said = next((t for s, _e, t in words if s == nearest and t), None)
                delta = (
                    f"  Δ{gap:+.2f}s" + (f" to {said!r}" if said else "")
                    if abs(gap) <= 0.6 else "  (no word onset within 0.6 s)"
                )
            picture = row.get("asset") or row.get("type") or "-"
            row_ids = [row.get("clip_id")] + [layer.get("clip_id") for layer in row.get("layers") or []]
            events.append((row["start"], 0, _marked(
                f"┃ CUT {row['index']}  {row.get('clip_id') or '(no picture clip)'} · {picture}{delta}",
                changed, *row_ids,
            )))
        for clip in clips:
            if _is_audio(clip, kinds) or clip_duration(clip) <= 0:
                continue
            c_start = start + _num(clip.get("at"))
            c_end = c_start + clip_duration(clip)
            label = str(clip.get("clipType") or "media")
            if isinstance(clip.get("asset"), str):
                label += f" {clip['asset']}"
            text = _clip_text(clip)
            if text:
                label += f' "{text}"'
            label += f" [{clip.get('id')}]"
            is_cut = (occurrence_id, round(c_start, 4)) in cut_rows and cut_rows[(occurrence_id, round(c_start, 4))].get("clip_id") == clip.get("id")
            if not is_cut:
                events.append((c_start, 1, _marked(f"+ {label}", changed, clip.get("id"))))
                if c_end < end - 0.5 / fps:
                    events.append((c_end, 1, _marked(f"− {label}", changed, clip.get("id"))))
            for at, what in clip_keyframes(clip, c_start, fps):
                if at > c_start + 0.5 / fps:
                    events.append((at, 2, f"◆ {clip.get('clipType')} {what}"))
        # Phrases: consecutive words not split by a pause or an event.
        event_times = sorted({round(t, 4) for t, _o, _x in events})
        occurrence_words = [word for word in all_words if start - _EPS <= word.start < end]
        # Timing-only words carry no text in the checkout; use the narration
        # text resolved by the same cut-table helper when counts agree.
        resolved = [text for _s, _e, text in sorted(words, key=lambda item: item[0])]
        if len(resolved) != len(occurrence_words):
            resolved = [None] * len(occurrence_words)
        spoken = [(word, word.text or resolved[index] or "·") for index, word in enumerate(occurrence_words)]
        phrases: list[tuple[float, float, list[tuple[Any, str]]]] = []
        for word, token in spoken:
            split = (
                not phrases
                or word.start - phrases[-1][1] >= pause
                or any(phrases[-1][1] - _EPS <= event <= word.start + _EPS for event in event_times)
            )
            if split:
                phrases.append((word.start, word.end, [(word, token)]))
            else:
                phrases[-1] = (phrases[-1][0], word.end, phrases[-1][2] + [(word, token)])
        block = [f"{_shot_name(shot, shot_id)} · {start:.2f}–{end:.2f} s · words: {source}"]
        merged = list(events)
        for phrase_start, phrase_end, group in phrases:
            tokens = " ".join(token for _word, token in group)
            merged.append((phrase_start, 3, f'│ "{tokens}"  [{phrase_start:.2f}–{phrase_end:.2f}]'))
            onsets = " · ".join(f"{token} {word.start:.2f}" for word, token in group)
            merged.append((phrase_start, 4, "\x01" + " " * 11 + f"{_id_range([word for word, _token in group])}  {onsets}"))
        previous_end = None
        for at, _order, text in sorted(merged, key=lambda item: (item[0], item[1])):
            if not _in_window(at, at + 1e-3, window):
                continue
            if text.startswith("│") and previous_end is not None and at - previous_end >= 1.0:
                block.append(f"{'':>8} │   … {at - previous_end:.2f} s without speech")
            if text.startswith("│"):
                previous_end = float(text.rsplit("–", 1)[1].rstrip("]"))
            if text.startswith("\x01"):
                block.append(text[1:])
                continue
            entry = f"{at:8.2f} {text}"
            block.append(entry if len(entry) <= width else entry[: width - 1] + "…")
        if len(block) > 1:
            lines.extend([""] + block)
    return "\n".join(lines)


# -------------------------------------------------------------------- code

def _py(value: Any, budget: int = 48) -> str:
    if isinstance(value, str):
        text = " ".join(value.split())
        return json.dumps(text if len(text) <= budget else text[: budget - 1] + "…", ensure_ascii=False)
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, list):
        if value and all(isinstance(item, list) and len(item) >= 2 and all(isinstance(x, (int, float)) for x in item[:2]) for item in value):
            return f"<{len(value)} timed words>"
        if len(value) > 4 or any(isinstance(item, (Mapping, list)) for item in value):
            return f"<list[{len(value)}]>"
        return "[" + ", ".join(_py(item, 16) for item in value) + "]"
    if isinstance(value, Mapping):
        if set(value) <= {"x", "y"} and all(isinstance(v, (int, float)) for v in value.values()):
            return f"({_py(value.get('x'))}, {_py(value.get('y'))})"
        inner = ", ".join(f"{k}={_py(v, 20)}" for k, v in list(value.items())[:4])
        return "{" + inner + (", …" if len(value) > 4 else "") + "}"
    return repr(value)


def _call(clip: Mapping[str, Any], full: bool = False) -> str:
    kind = str(clip.get("clipType") or "media").replace("-", "_")
    args = []
    if isinstance(clip.get("asset"), str):
        args.append(f"asset={json.dumps(clip['asset'])}")
    for key, value in _map(clip.get("params")).items():
        args.append(f"{key}={json.dumps(value, ensure_ascii=False) if full else _py(value)}")
    if clip.get("clipType") == "media" and clip.get("to") is not None:
        args.append(f"src={_num(clip.get('from')):.2f}..{_num(clip.get('to')):.2f}")
    return f"{kind}({', '.join(args)})"


def render_code(
    bundle: Mapping[str, Any],
    *,
    window: Sequence[float] | None = None,
    shot_ids: set[str] | None = None,
    width: int = 160,
    header: str = "",
    full: bool = False,
) -> str:
    """The composition as a readable program: shot → cut → element calls with resolved keyframes.

    ``full`` (``show --as code --detail``) prints every param value whole: nothing elided."""
    fps = _fps(bundle)
    table = build_cut_table(bundle)
    shots = _map(bundle.get("shots"))
    used: set[str] = set()
    body: list[str] = []
    for placement in _placements(bundle):
        shot_id = str(placement.get("shot_id") or "")
        if shot_ids and shot_id not in shot_ids:
            continue
        occurrence_id = str(placement.get("occurrence_id") or shot_id)
        start = _num(placement.get("at_ms", _map(placement.get("placement")).get("start_ms"))) / 1000.0
        length = _num(placement.get("duration_ms")) / 1000.0
        if not _in_window(start, start + length, window):
            continue
        shot = _map(shots.get(shot_id))
        internal = _internal(shot)
        kinds = _track_kinds(internal)
        clips = [_map(c) for c in _list(internal.get("clips")) if isinstance(c, Mapping)]
        words, _source = _words_for_occurrence(shot, start, clips, kinds)
        rows = [row for row in table["rows"] if row["occurrence_id"] == occurrence_id]
        body.append("")
        body.append(f'with shot({json.dumps(_shot_name(shot, shot_id), ensure_ascii=False)}, id="{shot_id}", at={start:.2f}, dur={length:.2f}):')
        for clip in sorted((c for c in clips if _is_audio(c, kinds)), key=lambda c: _num(c.get("at"))):
            c_start = start + _num(clip.get("at"))
            c_end = c_start + clip_duration(clip)
            if not _in_window(c_start, c_end, window):
                continue
            said = " ".join(t for s, _e, t in words if t and c_start - 1e-6 <= s < c_end)
            line = f"    {clip.get('track')}: {_call(clip, full)}  @ {c_start:.2f}–{c_end:.2f}"
            body.append(line + (f'  # "{said[:60]}{"…" if len(said) > 60 else ""}"' if said else ""))
        # Painter's order inside a cut: bottom track first (the shot lists tracks top -> bottom).
        depth = {str(_map(t).get("id")): -i for i, t in enumerate(_list(internal.get("tracks")))}
        visual = sorted(
            (c for c in clips if not _is_audio(c, kinds) and clip_duration(c) > 0),
            key=lambda c: (_num(c.get("at")), depth.get(str(c.get("track")), 0)),
        )
        for row in rows:
            if not _in_window(row["start"], row["end"], window):
                continue
            said = row.get("say") or ""
            body.append(
                f"    with cut({row['index']}, at={row['start']:.2f}, dur={row['duration']:.2f}):  # {row['tc_in']}"
                + (f' "{said[:70]}{"…" if len(said) > 70 else ""}"' if said else "")
            )
            members = [
                c for c in visual
                if row["start"] - 0.5 / fps <= start + _num(c.get("at")) < row["end"] - 0.5 / fps
            ]
            if not members:
                body.append("        pass  # layers continue from earlier cuts")
            for clip in members:
                used.add(str(clip.get("clipType") or "media"))
                c_start = start + _num(clip.get("at"))
                c_end = c_start + clip_duration(clip)
                timing = f"+{c_start - row['start']:.2f}" if c_start > row["start"] + 0.5 / fps else ""
                span = "" if abs(c_end - row["end"]) < 0.5 / fps else f" until {c_end:.2f}"
                line = f"        {str(clip.get('track')):<6} = {_call(clip, full)}"
                suffix = "  # " + " ".join(part for part in (f"enters {timing}" if timing else "", span.strip(), f"id {clip.get('id')}") if part)
                text = line + suffix
                body.append(text if full or len(text) <= width else line[: width - len(suffix) - 1] + "…" + suffix)
                for at, what in clip_keyframes(clip, c_start, fps):
                    body.append(f"            # ◆ {at:.2f} {what}")
    legend = [header] if header else []
    legend.append(f"# {fps:g} fps · times are timeline seconds · ◆ = element keyframe (clip frame → timeline seconds)")
    legend.append("# element sources (props are each element's schema in element.yaml beside it):")
    for name in sorted(used):
        legend.append(f"#   {name:<14} {element_source(name) or '(not in this checkout)'}")
    return "\n".join(legend + body)


# Canonical V3 contributed views share this public inspection surface.
from astrid.core.pack.views import (  # noqa: E402,F401
    UnsupportedView,
    discover_views,
    read_view,
    render_view_text,
    resolve_view,
)
