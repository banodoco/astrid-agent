#!/usr/bin/env python3
"""Join ordered pixel-art frames into an am-sprite-compatible strip."""

from __future__ import annotations

from astrid.pack_api import add_choice_arg
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("pixel.strip")

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import AstridError
from astrid.packs.pixel.actions import _common as px

ALIGNS = ("bottom", "center", "top")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pixel.strip",
        description="Join ordered pixel frames into a shared-grid sprite strip.",
    )
    parser.add_argument("--frame", action="append", type=Path, required=True, help="Ordered managed image frame (repeatable).")
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    parser.add_argument("--grid-width", type=int, default=0)
    parser.add_argument("--grid-height", type=int, default=0)
    add_choice_arg(parser, "--fit", values=("cover", "contain", "none"), default="cover")
    parser.add_argument(
        "--palette",
        action="append",
        default=None,
        help="One palette for every frame; repeat hex colours or pass a comma-separated list.",
    )
    parser.add_argument("--max-colors", type=int, default=0)
    parser.add_argument("--fps", type=float, default=8.0)
    add_choice_arg(parser, "--align", values=ALIGNS, default="bottom")
    parser.add_argument("--allow-lossy", type=px.parse_bool, default=False, help="Permit a coarse explicit grid (true/false).")
    return parser


def _palette_text(values: list[str] | None) -> str | None:
    if not values:
        return None
    return ",".join(item for value in values for item in value.split(",") if item.strip())


def align_frames(
    frames: list[np.ndarray], *, align: str = "bottom"
) -> tuple[list[np.ndarray], list[dict[str, int]]]:
    if align not in ALIGNS:
        raise AstridError(
            f"align must be one of {', '.join(ALIGNS)}",
            recovery_command="use --align bottom",
        )
    cell_h = max(frame.shape[0] for frame in frames)
    cell_w = max(frame.shape[1] for frame in frames)
    placed: list[np.ndarray] = []
    placements: list[dict[str, int]] = []
    for frame in frames:
        height, width = frame.shape[:2]
        dx = (cell_w - width) // 2
        dy = {"bottom": cell_h - height, "center": (cell_h - height) // 2, "top": 0}[align]
        placements.append({"width": width, "height": height, "x": dx, "y": dy})
        if (height, width) == (cell_h, cell_w):
            placed.append(frame)
            continue
        canvas = np.zeros((cell_h, cell_w, 4), dtype=np.uint8)
        canvas[dy:dy + height, dx:dx + width] = frame
        placed.append(canvas)
    return placed, placements


def build_strip(
    frames: list[np.ndarray],
    *,
    grid_width: int = 0,
    grid_height: int = 0,
    fit: str = "cover",
    palette: str | None = None,
    max_colors: int = 0,
    align: str = "bottom",
    allow_lossy: bool = False,
) -> np.ndarray:
    if len(frames) < 2:
        raise AstridError(f"pixel.strip needs at least 2 frames (got {len(frames)})")
    if (grid_width > 0) != (grid_height > 0):
        raise AstridError("grid_width and grid_height must be set together")
    aligned, _placements = align_frames(frames, align=align)
    if grid_width <= 0:
        grid_height, grid_width = aligned[0].shape[:2]
    native_frames = []
    for frame in aligned:
        native, _preview, _report = px.snap_image(
            frame,
            grid_width=int(grid_width),
            grid_height=int(grid_height),
            fit=fit,
            palette=palette,
            max_colors=int(max_colors or 0),
            scale=1,
            dither=False,
            allow_lossy=allow_lossy,
        )
        native_frames.append(native)
    return np.concatenate(native_frames, axis=1)


def build_metadata(
    *,
    frame_w: int,
    frame_h: int,
    count: int,
    fps: float,
    sources: list[str],
    fit: str,
    placements: list[dict[str, int]],
    align: str,
    allow_lossy: bool = False,
) -> dict:
    frames = [
        {"index": i, "source": name, "x": i * frame_w, "placed": placements[i]}
        for i, name in enumerate(sources)
    ]
    sprite = {"frameWidth": frame_w, "frameHeight": frame_h, "count": count, "fps": fps}
    return {
        "frameWidth": frame_w,
        "frameHeight": frame_h,
        "count": count,
        "fps": fps,
        "grid": {"width": frame_w, "height": frame_h},
        "fit": fit,
        "align": align,
        "allow_lossy": allow_lossy,
        "frames": frames,
        "am_sprite_frames": sprite,
    }


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        if len(args.frame) < 2:
            raise AstridError("pixel.strip needs at least 2 frames")
        if args.fps <= 0:
            raise AstridError("fps must be greater than 0")
        sources = [path.expanduser().resolve() for path in args.frame]
        for source in sources:
            if not source.is_file():
                raise AstridError(f"frame not found: {source}")
        out_dir = args.out.expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        palette = _palette_text(args.palette)
        frames = [px.load_rgba(str(source)) for source in sources]
        _aligned, placements = align_frames(frames, align=args.align)
        strip = build_strip(
            frames,
            grid_width=int(args.grid_width),
            grid_height=int(args.grid_height),
            fit=args.fit,
            palette=palette,
            max_colors=int(args.max_colors or 0),
            align=args.align,
            allow_lossy=bool(args.allow_lossy),
        )
        frame_h, total_w = strip.shape[:2]
        frame_w = total_w // len(sources)
        metadata = build_metadata(
            frame_w=frame_w,
            frame_h=frame_h,
            count=len(sources),
            fps=float(args.fps),
            sources=[source.name for source in sources],
            fit=args.fit,
            placements=placements,
            align=args.align,
            allow_lossy=bool(args.allow_lossy),
        )
        px.save_rgba(strip, str(out_dir / "strip.png"))
        (out_dir / "strip.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest = build_manifest(
            kind="pixel.strip",
            inputs={
                "frames": [source.name for source in sources],
                "grid": [args.grid_width, args.grid_height],
                "fit": args.fit,
                "palette": palette,
                "max_colors": int(args.max_colors or 0),
                "fps": float(args.fps),
                "align": args.align,
                "allow_lossy": bool(args.allow_lossy),
            },
            outputs=[
                {"name": "strip", "path": "strip.png", "type": "file", "artifact_type": "image", "role": "result", "is_primary": True},
                {"name": "metadata", "path": "strip.json", "type": "file", "role": "auxiliary"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(out_dir / "manifest.json", manifest)
        return 0

    return run_pack_main("pixel.strip", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
