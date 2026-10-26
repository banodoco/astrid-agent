"""Attach, list and validate data tracks (``app.data.<name>``) in a V3 checked-out timeline document.

Works on the JSON that ``astrid timelines document checkout`` writes; publish
the result with ``astrid timelines document check`` then ``publish``. Every layer and
check reads tracks through ``motion.data.tracks`` (see the visualize
extension guide).

    python3 -m astrid.packs.rendering.docs.scripts.timeline_data list --file /tmp/edit.json
    python3 -m astrid.packs.rendering.docs.scripts.timeline_data add --file /tmp/edit.json \\
        --clip v17-04-am-sprite --name claw_contact --track track.json
    python3 -m astrid.packs.rendering.docs.scripts.timeline_data loudness --file /tmp/edit.json
    python3 -m astrid.packs.rendering.docs.scripts.timeline_data validate --file /tmp/edit.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

from astrid.packs.rendering.actions.timeline_visualize.motion import data

PRODUCER_LOUDNESS = "astrid.data.loudness@1"


def _clips(bundle: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """``(shot, internal timeline, clip)`` for every clip in the document."""
    for shot in (bundle.get("shots") or {}).values():
        internal = shot.get("internal_timeline") or shot.get("base_internal_timeline") or {}
        for clip in internal.get("clips") or []:
            if isinstance(clip, dict):
                yield shot, internal, clip


def _find_clip(bundle: dict[str, Any], clip_id: str) -> dict[str, Any]:
    matches = [clip for _s, _i, clip in _clips(bundle) if clip.get("id") == clip_id]
    if len(matches) != 1:
        known = sorted(str(clip.get("id")) for _s, _i, clip in _clips(bundle))[:20]
        raise SystemExit(f"error: clip {clip_id!r} matched {len(matches)} clips; some ids: {', '.join(known)}")
    return matches[0]


def cmd_list(args: argparse.Namespace) -> int:
    bundle = json.loads(args.file.read_text(encoding="utf-8"))
    count = 0
    for _shot, _internal, clip in _clips(bundle):
        for name, raw in ((clip.get("app") or {}).get("data") or {}).items():
            problems = data.validate(raw)
            items = raw.get("items") if isinstance(raw, dict) else None
            size = len(items.get("values") or []) if isinstance(items, dict) else len(items or [])
            source = (raw.get("source") or {}) if isinstance(raw, dict) else {}
            print(f"{clip.get('id')}: {name} {raw.get('kind') if isinstance(raw, dict) else '?'} "
                  f"({size} items, {raw.get('units', '') if isinstance(raw, dict) else ''}) "
                  f"from {source.get('producer') or source.get('handle') or 'unknown'}"
                  + (f"  INVALID: {'; '.join(problems)}" if problems else ""))
            count += 1
    print(f"{count} data track(s)")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    bundle = json.loads(args.file.read_text(encoding="utf-8"))
    bad = 0
    for _shot, _internal, clip in _clips(bundle):
        for name, raw in ((clip.get("app") or {}).get("data") or {}).items():
            for problem in data.validate(raw):
                print(f"{clip.get('id')}: app.data.{name}: {problem}")
                bad += 1
    print("ok" if not bad else f"{bad} problem(s)")
    return 0 if not bad else 1


def cmd_add(args: argparse.Namespace) -> int:
    bundle = json.loads(args.file.read_text(encoding="utf-8"))
    clip = _find_clip(bundle, args.clip)
    track = json.loads(Path(args.track).read_text(encoding="utf-8"))
    data.attach(clip, args.name, track)
    args.file.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"file": str(args.file), "clip": args.clip, "track": args.name, "kind": track.get("kind")}))
    return 0


def _rms_db(samples, rate: int, step: float) -> list[float]:
    per = max(1, int(rate * step))
    values = []
    for begin in range(0, len(samples), per):
        chunk = samples[begin:begin + per]
        power = sum(v * v for v in chunk) / len(chunk) if chunk else 0.0
        values.append(round(10 * math.log10(power), 1) if power > 1e-12 else -120.0)
    return values


def loudness_track(path: Path, *, offset: float, seconds: float, step: float, handle: str) -> dict[str, Any]:
    """An RMS dBFS series (clip time) for ``seconds`` of the WAV at ``path`` from ``offset``."""
    from astrid.packs.rendering.actions.timeline_visualize.motion.audio import _read_window

    samples, rate = _read_window(path, offset, seconds)
    return data.make_track(
        "series", {"t0": 0.0, "step": step, "values": _rms_db(samples, rate, step)}, units="dBFS", time="clip",
        source={"handle": handle, "digest": handle, "producer": PRODUCER_LOUDNESS,
                "params": {"step_s": step, "window": "rms"}},
    )


def cmd_loudness(args: argparse.Namespace) -> int:
    from astrid.sdk import AstridClient

    bundle = json.loads(args.file.read_text(encoding="utf-8"))
    lanes = {lane.strip() for lane in args.tracks.split(",") if lane.strip()}
    done = 0
    with AstridClient.open_from_launcher(start_pack_host=False) as client, tempfile.TemporaryDirectory() as scratch:
        media = getattr(client, "media", None)
        reader = getattr(media, "read_bytes", None) or getattr(getattr(client, "_remote", client).media, "read_bytes")
        cache: dict[str, Path] = {}
        for _shot, internal, clip in _clips(bundle):
            track_id = str(clip.get("track") or "")
            if track_id not in lanes or not clip.get("asset"):
                continue
            entry = ((internal.get("registry") or {}).get("assets") or {}).get(clip["asset"]) or {}
            digest = str(entry.get("content_sha256") or entry.get("digest") or entry.get("media_id") or "")
            digest = "sha256:" + digest.removeprefix("sha256:")
            if len(digest) != 71:
                print(f"skip {clip.get('id')}: no media digest in the registry", file=sys.stderr)
                continue
            if digest not in cache:
                cache[digest] = Path(scratch) / digest[7:19]
                cache[digest].write_bytes(reader(digest))
            start = float(clip.get("from") or 0.0)
            length = (float(clip["to"]) - start) if clip.get("to") is not None else float(clip.get("hold") or 0.0)
            try:
                track = loudness_track(cache[digest], offset=start, seconds=length, step=args.step, handle=digest)
            except Exception as exc:  # noqa: BLE001 - name the clip, keep going
                print(f"skip {clip.get('id')}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            data.attach(clip, "loudness", track)
            done += 1
    args.file.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"file": str(args.file), "loudness_tracks": done, "producer": PRODUCER_LOUDNESS}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="timeline_data", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, extra in (("list", cmd_list, None), ("validate", cmd_validate, None),
                                 ("add", cmd_add, "add"), ("loudness", cmd_loudness, "loudness")):
        sub = commands.add_parser(name)
        sub.add_argument("--file", required=True, type=Path, help="the checkout JSON (astrid timelines document checkout)")
        if extra == "add":
            sub.add_argument("--clip", required=True, help="clip id in the document (timelines show prints them)")
            sub.add_argument("--name", required=True, help="track name, e.g. face, claw_contact, loudness")
            sub.add_argument("--track", required=True, help="JSON file holding {kind, units, time, source, items}")
        if extra == "loudness":
            sub.add_argument("--tracks", default="vo,music", help="audio tracks to measure (default vo,music)")
            sub.add_argument("--step", type=float, default=0.05, help="seconds per value (default 0.05)")
        sub.set_defaults(handler=handler)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

