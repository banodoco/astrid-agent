#!/usr/bin/env python3
"""Tile a video into an MxN grid of overlapping spatial crops."""


from __future__ import annotations

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('foley.tile_video')
import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.pack_api import pack_die
from astrid.pack_api import run_pack_main


def _die(message: str) -> None:
    pack_die(message, recovery_command="fix the foley.tile_video inputs and rerun the command")


def _parse_grid(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*([1-9][0-9]*)\s*[xX]\s*([1-9][0-9]*)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("--grid must be COLSxROWS, e.g. 4x4")
    return int(match.group(1)), int(match.group(2))


class FoleyProbeError(ValueError):
    """Raised when ffprobe JSON for the input video is missing required fields.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers in
    callers still catch the typed error, while still being uniquely
    discriminable for tests / structured logging.
    """


def _ffprobe(video: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,duration:format=duration",
        "-of", "json",
        str(video),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        raise FoleyProbeError(
            f"ffprobe returned no video streams for {video}"
        )
    stream = streams[0]
    stream_duration = stream.get("duration")
    if stream_duration is None:
        fmt = data.get("format")
        if not isinstance(fmt, dict):
            raise FoleyProbeError(
                f"ffprobe returned no 'format' block for {video}"
            )
        fmt_duration = fmt.get("duration")
        if fmt_duration is None:
            raise FoleyProbeError(
                f"ffprobe returned no duration on stream or format for {video}"
            )
        try:
            duration = float(fmt_duration)
        except (TypeError, ValueError) as exc:
            raise FoleyProbeError(
                f"ffprobe format.duration {fmt_duration!r} is not numeric for {video}"
            ) from exc
    else:
        try:
            duration = float(stream_duration)
        except (TypeError, ValueError) as exc:
            raise FoleyProbeError(
                f"ffprobe stream duration {stream_duration!r} is not numeric for {video}"
            ) from exc
    num, den = stream["r_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "duration": duration,
        "fps": fps,
    }


def _tile_rects(W: int, H: int, cols: int, rows: int, overlap: float) -> list[dict[str, Any]]:
    """Compute pixel rects for each tile. Edge tiles clamp to the frame."""
    base_w = W / cols
    base_h = H / rows
    tile_w = base_w * (1.0 + overlap)
    tile_h = base_h * (1.0 + overlap)
    rects: list[dict[str, Any]] = []
    for r in range(rows):
        for c in range(cols):
            cx = (c + 0.5) * base_w
            cy = (r + 0.5) * base_h
            x = max(0, int(round(cx - tile_w / 2)))
            y = max(0, int(round(cy - tile_h / 2)))
            x2 = min(W, int(round(cx + tile_w / 2)))
            y2 = min(H, int(round(cy + tile_h / 2)))
            w = (x2 - x) // 2 * 2  # libx264 wants even dimensions
            h = (y2 - y) // 2 * 2
            rects.append({
                "row": r, "col": c,
                "rect": [x, y, w, h],
                "rect_norm": [x / W, y / H, w / W, h / H],
            })
    return rects


def _crop_tile(video: Path, rect: list[int], out_clip: Path, *, trim: float | None, force: bool) -> None:
    if out_clip.exists() and not force:
        return
    out_clip.parent.mkdir(parents=True, exist_ok=True)
    x, y, w, h = rect
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if trim is not None:
        cmd += ["-t", f"{trim:.3f}"]
    cmd += [
        "-i", str(video),
        "-filter:v", f"crop={w}:{h}:{x}:{y}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-an",
        str(out_clip),
    ]
    subprocess.run(cmd, check=True)


def _first_frame(clip: Path, out_png: Path, force: bool) -> None:
    if out_png.exists() and not force:
        return
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(clip),
        "-frames:v", "1",
        "-q:v", "2",
        str(out_png),
    ]
    subprocess.run(cmd, check=True)


def _global_first_frame(video: Path, out_png: Path, force: bool) -> None:
    if out_png.exists() and not force:
        return
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
        "-frames:v", "1",
        "-q:v", "2",
        str(out_png),
    ]
    subprocess.run(cmd, check=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Crop a video into an MxN grid of overlapping spatial tiles.")
    p.add_argument("--video", type=Path, required=True, help="Source video.")
    p.add_argument("--out", type=Path, required=True, help="Output directory.")
    p.add_argument("--grid", type=_parse_grid, default=(4, 4), help="COLSxROWS, e.g. 4x4 (default 4x4).")
    p.add_argument("--overlap", type=float, default=0.25, help="Tile overlap fraction (0..1, default 0.25).")
    p.add_argument("--trim", type=float, default=None, help="Trim each tile clip to this many seconds (default: full length).")
    p.add_argument("--force", action="store_true", help="Re-render tiles/frames even if they exist.")
    p.add_argument("--dry-run", action="store_true", help="Plan and write tiles.json with rects, skip ffmpeg work.")
    return p


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        if not 0.0 <= args.overlap < 1.0:
            _die(f"--overlap must be in [0, 1), got {args.overlap}")
        video = args.video.expanduser().resolve()
        if not video.is_file():
            _die(f"video not found: {video}")
        out_root = args.out.expanduser().resolve()
        out_root.mkdir(parents=True, exist_ok=True)

        probe = _ffprobe(video)
        cols, rows = args.grid
        rects = _tile_rects(probe["width"], probe["height"], cols, rows, args.overlap)

        duration = probe["duration"]
        trimmed = min(args.trim, duration) if args.trim is not None else duration

        tiles: list[dict[str, Any]] = []
        for entry in rects:
            r, c = entry["row"], entry["col"]
            tile_id = f"tile_{r}_{c}"
            clip_rel = Path("tiles") / f"{r}_{c}.mp4"
            frame_rel = Path("frames") / f"{r}_{c}.png"
            clip_abs = out_root / clip_rel
            frame_abs = out_root / frame_rel
            if not args.dry_run:
                _crop_tile(video, entry["rect"], clip_abs, trim=args.trim, force=args.force)
                _first_frame(clip_abs, frame_abs, args.force)
            tiles.append({
                "id": tile_id,
                "row": r,
                "col": c,
                "rect": entry["rect"],
                "rect_norm": entry["rect_norm"],
                "tile_clip": str(clip_rel),
                "first_frame": str(frame_rel),
            })

        global_frame_rel = Path("frames") / "global.png"
        if not args.dry_run:
            _global_first_frame(video, out_root / global_frame_rel, args.force)

        manifest = {
            "video": str(video),
            "video_size": [probe["width"], probe["height"]],
            "duration": duration,
            "trimmed_duration": trimmed,
            "fps": probe["fps"],
            "grid": {"cols": cols, "rows": rows, "overlap": args.overlap},
            "global_first_frame": str(global_frame_rel),
            "tiles": tiles,
        }
        manifest_path = out_root / "tiles.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"wrote_tiles_manifest={manifest_path}")
        print(f"wrote_tiles={len(tiles)}")

        # --- universal result manifest -----------------------
        result_manifest_path = out_root / "manifest.json"
        result_manifest = build_manifest(
            kind="tile_video",
            inputs={
                "video": str(video),
                "grid": [cols, rows],
                "overlap": args.overlap,
                "trim": args.trim,
            },
            outputs=[
                {"path": "tiles.json", "type": "file"},
                {"path": "tiles", "type": "directory"},
                {"path": "frames", "type": "directory"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(result_manifest_path, result_manifest)
        # -------------------------------------------------------------------------

        return 0

    return run_pack_main("foley.tile_video", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
