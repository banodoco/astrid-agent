#!/usr/bin/env python3
from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.inspect_cut')


import argparse
import json
import sys
import textwrap
from pathlib import Path

from astrid.packs.editorial.shared.hype import enriched_arrangement

TRACKS = ("a1", "v1", "v2")
TRACK_FILL = {"a1": "A", "v1": "V", "v2": "O"}
ZONE_FILL = {
    ("a1", enriched_arrangement.ZoneKind.AUDIO_DEAD): "░",
    ("v1", enriched_arrangement.ZoneKind.VIDEO_DEAD): "▒",
    ("v2", enriched_arrangement.ZoneKind.VIDEO_DEAD): "▒",
}
RESET = "\033[0m"
COLORS = {"title": "\033[1m", "muted": "\033[2m", "warn": "\033[33m"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect a cut run directory.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--clip", type=int)
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"Run directory not found: {run_dir}")
    enriched = enriched_arrangement.load(run_dir)
    payload = build_report(enriched, _load_refine_report(run_dir / "refine.json"), clip_order=args.clip)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_text(payload, use_color=not args.no_color))
    return 0


def build_report(enriched: enriched_arrangement.EnrichedArrangement, refine_report: dict[str, object], *, clip_order: int | None = None) -> dict[str, object]:
    clip_index = _build_clip_index(refine_report)
    payload: dict[str, object] = {
        "run_dir": str(enriched.run_dir),
        "script": _script_entries(enriched, clip_index),
        "structure": _structure_payload(enriched),
    }
    if clip_order is not None:
        clip = enriched.clips_by_order.get(int(clip_order))
        if clip is None:
            raise SystemExit(f"Clip {clip_order} not found in arrangement")
        payload["clip"] = _clip_payload(clip, clip_index.get(int(clip_order), {}))
    return payload


def render_text(payload: dict[str, object], *, use_color: bool) -> str:
    parts = [_render_script(payload["script"], use_color), _render_structure(payload["structure"], use_color)]
    if payload.get("clip") is not None:
        parts.append(_render_clip(payload["clip"], use_color))
    return "\n\n".join(parts)


def _render_script(entries: list[dict[str, object]], use_color: bool) -> str:
    lines = [_paint("SCRIPT", "title", use_color)]
    for entry in entries:
        prefix = f"[{entry['order']}] uuid={entry['uuid']}"
        lines.append(f"{prefix} {entry['text']}")
        for warning in entry["warnings"]:
            label = _paint(f"! {warning['code']}", "warn", use_color)
            wrapped = textwrap.wrap(str(warning["message"]), width=84) or [""]
            lines.append(f"    {label}: {wrapped[0]}")
            lines.extend(f"      {line}" for line in wrapped[1:])
    if len(lines) == 1:
        lines.append(_paint("(no dialogue clips)", "muted", use_color))
    return "\n".join(lines)


def _render_structure(payload: dict[str, object], use_color: bool) -> str:
    lines = [_paint("STRUCTURE", "title", use_color), f"duration={payload['duration_sec']:.2f}s width={payload['width']}", f"time {payload['scale']}"]
    lines.extend(f"{track:>2}  {payload['tracks'][track]}" for track in TRACKS)
    lines.append("legend A/V/O=clip  S=stinger  ░=audio_dead overlap  ▒=video_dead overlap")
    if payload.get("clips"):
        lines.append("clips:")
        lines.extend(
            f"  [{entry['order']}] uuid={entry['uuid']} kind={entry['kind']} asset={entry['asset_key']}"
            for entry in payload["clips"]
        )
    return "\n".join(lines)


def _render_clip(payload: dict[str, object], use_color: bool) -> str:
    lines = [
        _paint(f"CLIP {payload['order']} uuid={payload['uuid']}", "title", use_color),
        f"kind={payload['kind']} asset={payload['asset_key']}",
        f"trim_range={payload['trim_range']}",
        f"current_transcript={payload['current_transcript'] or '(none)'}",
    ]
    if payload["before_transcript"] or payload["after_transcript"]:
        lines.append(f"before_transcript={payload['before_transcript'] or '(none)'}")
        lines.append(f"after_transcript={payload['after_transcript'] or '(none)'}")
    lines.append("findings:")
    if payload["findings"]:
        lines.extend(f"  - {entry['code']}: {entry['message']}" for entry in payload["findings"])
    else:
        lines.append("  - none")
    lines.append("zone_overlaps:")
    if payload["zone_overlaps"]:
        lines.extend(
            f"  - {entry['track']} {entry['kind']} timeline={entry['timeline_start']:.2f}-{entry['timeline_end']:.2f} source={entry['source_start']:.2f}-{entry['source_end']:.2f}"
            for entry in payload["zone_overlaps"]
        )
    else:
        lines.append("  - none")
    lines.append("fix_options:")
    lines.extend((f"  {line}" for line in json.dumps(payload["fix_options"], indent=2).splitlines())) if payload["fix_options"] else lines.append("  none")
    return "\n".join(lines)


def _script_entries(enriched: enriched_arrangement.EnrichedArrangement, clip_index: dict[int, dict[str, object]]) -> list[dict[str, object]]:
    entries = []
    for clip in sorted(enriched.clips, key=lambda item: item.order):
        warnings = list((clip_index.get(clip.order) or {}).get("findings", []))
        if _is_dialogue(clip):
            entries.append({"order": clip.order, "uuid": clip.uuid, "kind": "dialogue", "text": _current_transcript(clip) or _pool_text(clip) or "(no transcript)", "warnings": warnings})
        elif _is_stinger(clip):
            entries.append({"order": clip.order, "uuid": clip.uuid, "kind": "stinger", "text": _stinger_placeholder(clip), "warnings": warnings})
    return entries


def _structure_payload(enriched: enriched_arrangement.EnrichedArrangement) -> dict[str, object]:
    width = 72
    duration_sec = max((_timeline_end(clip) for clip in (enriched.timeline or {}).get("clips", [])), default=0.0)
    tracks = {track: [" "] * width for track in TRACKS}
    for clip in enriched.clips:
        for track, timeline_clip in _clip_track_map(clip).items():
            if timeline_clip is None:
                continue
            _paint_segment(tracks[track], _timeline_start(timeline_clip), _timeline_end(timeline_clip), duration_sec, width, _track_fill(track, clip))
            for overlap in _timeline_zone_overlaps(track, clip):
                _paint_segment(tracks[track], overlap["timeline_start"], overlap["timeline_end"], duration_sec, width, ZONE_FILL[(track, enriched_arrangement.ZoneKind(overlap["kind"]))])
    return {
        "duration_sec": round(duration_sec, 6),
        "width": width,
        "scale": _scale_line(duration_sec, width),
        "tracks": {track: "".join(chars) for track, chars in tracks.items()},
        "clips": [
            {"order": clip.order, "uuid": clip.uuid, "kind": _clip_kind(clip), "asset_key": clip.asset_key}
            for clip in sorted(enriched.clips, key=lambda item: item.order)
        ],
    }


def _clip_payload(clip: enriched_arrangement.EnrichedClip, clip_info: dict[str, object]) -> dict[str, object]:
    trim_range = None if not isinstance(clip.clip.get("audio_source"), dict) else list(clip.clip["audio_source"].get("trim_sub_range") or [])
    fix_options = clip_info.get("auto_fix")
    before = fix_options.get("source_transcript_text_before") if isinstance(fix_options, dict) else None
    after = fix_options.get("source_transcript_text_after") if isinstance(fix_options, dict) else None
    overlaps = [entry for track in TRACKS for entry in _timeline_zone_overlaps(track, clip)]
    return {
        "order": clip.order,
        "uuid": clip.uuid,
        "kind": _clip_kind(clip),
        "asset_key": clip.asset_key,
        "trim_range": trim_range,
        "current_transcript": _current_transcript(clip) or _pool_text(clip),
        "before_transcript": before,
        "after_transcript": after,
        "findings": list(clip_info.get("findings", [])),
        "zone_overlaps": overlaps,
        "fix_options": fix_options,
    }


def _build_clip_index(refine_report: dict[str, object]) -> dict[int, dict[str, object]]:
    index: dict[int, dict[str, object]] = {}
    for entry in _audio_auto_fixes(refine_report):
        order = int(entry.get("order") or 0)
        if order > 0:
            item = index.setdefault(order, {"findings": []})
            issues = entry.get("issues_resolved") or []
            item["findings"].append({"code": str(issues[0]) if issues else "audio_boundary", "message": "Auto-fix available from audio boundary review."})
            item["auto_fix"] = dict(entry)
    flags = refine_report.get("flags") or {}
    if isinstance(flags, dict):
        for entries in flags.values():
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and int(entry.get("order") or 0) > 0:
                    index.setdefault(int(entry["order"]), {"findings": []})["findings"].append({"code": str(entry.get("code") or "flag"), "message": str(entry.get("message") or "")})
    for entry in refine_report.get("rejected_nudges") or []:
        if isinstance(entry, dict) and int(entry.get("order") or 0) > 0:
            index.setdefault(int(entry["order"]), {"findings": []})["findings"].append({"code": str(entry.get("reason") or "rejected_nudge"), "message": str(entry.get("message") or "")})
    return index


def _audio_auto_fixes(refine_report: dict[str, object]) -> list[dict[str, object]]:
    auto_fixes = refine_report.get("auto_fixes")
    if isinstance(auto_fixes, dict) and isinstance(auto_fixes.get("audio_boundary"), list):
        return [entry for entry in auto_fixes["audio_boundary"] if isinstance(entry, dict)]
    return [entry for entry in refine_report.get("per_clip") or [] if isinstance(entry, dict)]


def _load_refine_report(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _clip_track_map(clip: enriched_arrangement.EnrichedClip) -> dict[str, dict[str, object] | None]:
    return {"a1": clip.audio_timeline_clip, "v1": clip.primary_visual_timeline_clip, "v2": clip.overlay_timeline_clip}


def _timeline_zone_overlaps(track: str, clip: enriched_arrangement.EnrichedClip) -> list[dict[str, object]]:
    timeline_clip = _clip_track_map(clip).get(track)
    if timeline_clip is None:
        return []
    zone_kind = enriched_arrangement.ZoneKind.AUDIO_DEAD if track == "a1" else enriched_arrangement.ZoneKind.VIDEO_DEAD
    clip_from, clip_to, clip_at = _clip_from(timeline_clip), _clip_to(timeline_clip), _timeline_start(timeline_clip)
    overlaps = []
    for zone in clip.zones:
        if zone.kind is not zone_kind:
            continue
        start, end = max(float(zone.start), clip_from), min(float(zone.end), clip_to)
        if end > start:
            overlaps.append({"track": track, "kind": zone.kind.value, "source_start": round(start, 6), "source_end": round(end, 6), "timeline_start": round(clip_at + (start - clip_from), 6), "timeline_end": round(clip_at + (end - clip_from), 6)})
    return overlaps


def _current_transcript(clip: enriched_arrangement.EnrichedClip) -> str | None:
    audio_source = clip.clip.get("audio_source")
    trim = audio_source.get("trim_sub_range") if isinstance(audio_source, dict) else None
    if not isinstance(trim, list) or len(trim) != 2:
        return None
    start, end = float(trim[0]), float(trim[1])
    joined = " ".join(
        text for segment in clip.transcript_segments
        for text in [str(segment.get("text", "")).strip()]
        if float(segment.get("end", 0.0)) > start and float(segment.get("start", 0.0)) < end and text
    ).strip()
    return joined or None


def _pool_text(clip: enriched_arrangement.EnrichedClip) -> str | None:
    text = str((clip.audio_pool_entry or clip.pool_entry or {}).get("text", "")).strip()
    return text or None


def _is_dialogue(clip: enriched_arrangement.EnrichedClip) -> bool:
    return bool(clip.audio_pool_entry and clip.audio_pool_entry.get("category") == "dialogue")


def _is_stinger(clip: enriched_arrangement.EnrichedClip) -> bool:
    return not _is_dialogue(clip) and (clip.clip.get("visual_source") or {}).get("role") == "stinger"


def _stinger_placeholder(clip: enriched_arrangement.EnrichedClip) -> str:
    text = (clip.clip.get("text_overlay") or {}).get("content")
    return f"[visual stinger] {text}" if text else "[visual stinger]"


def _clip_kind(clip: enriched_arrangement.EnrichedClip) -> str:
    return "dialogue" if _is_dialogue(clip) else str((clip.clip.get("visual_source") or {}).get("role") or "visual")


def _track_fill(track: str, clip: enriched_arrangement.EnrichedClip) -> str:
    return "S" if track == "v2" and _is_stinger(clip) else TRACK_FILL[track]


def _paint_segment(chars: list[str], start: float, end: float, total: float, width: int, fill: str) -> None:
    if width <= 0:
        return
    if total <= 0.0:
        chars[0] = fill
        return
    start_idx = max(0, min(width - 1, int((start / total) * width)))
    end_idx = max(start_idx + 1, min(width, int((end / total) * width) or start_idx + 1))
    for idx in range(start_idx, end_idx):
        chars[idx] = fill


def _scale_line(duration_sec: float, width: int) -> str:
    if duration_sec <= 0.0:
        return "0s"
    labels = [" "] * width
    tick_count = min(6, max(2, width // 12))
    for tick in range(tick_count):
        pos = min(width - 1, round((tick / max(tick_count - 1, 1)) * (width - 1)))
        label = f"{round((tick / max(tick_count - 1, 1)) * duration_sec):.0f}s"
        for offset, char in enumerate(label):
            if pos + offset < width:
                labels[pos + offset] = char
    return "".join(labels).rstrip()


def _timeline_start(clip: dict[str, object]) -> float:
    return float(clip.get("at", 0.0))


def _timeline_end(clip: dict[str, object]) -> float:
    return _timeline_start(clip) + max(0.0, _clip_to(clip) - _clip_from(clip))


def _clip_from(clip: dict[str, object]) -> float:
    return float(clip.get("from", clip.get("from_", 0.0)))


def _clip_to(clip: dict[str, object]) -> float:
    return float(clip.get("to", clip.get("to_", 0.0)))


def _paint(text: str, style: str, use_color: bool) -> str:
    return f"{COLORS[style]}{text}{RESET}" if use_color else text


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
