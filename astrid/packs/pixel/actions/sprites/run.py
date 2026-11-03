#!/usr/bin/env python3
"""Turn one generated sheet into separate native RGBA sprites."""

from __future__ import annotations

from astrid.pack_api import add_choice_arg
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("pixel.sprites")

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import AstridError
from astrid.packs.pixel.actions import _common as px


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pixel.sprites", description="Cut, snap and split a generated sprite sheet.")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    add_choice_arg(parser, "--mode", values=("chroma", "flat", "luma"), default="chroma")
    parser.add_argument("--key", default="#FF00FF")
    parser.add_argument("--tolerance", type=int, default=48)
    parser.add_argument("--holes", type=px.parse_bool, default=True)
    parser.add_argument("--block", type=float, default=None)
    parser.add_argument("--cells", default=None)
    parser.add_argument("--merge-distance", type=int, default=3)
    parser.add_argument("--padding", type=int, default=2)
    parser.add_argument("--scale", type=int, default=8)
    return parser


def make_sprites(source, *, mode="chroma", key="#FF00FF", tolerance=48, holes=True,
                 block=None, cells=None, merge_distance=3, padding=2, scale=8):
    if scale < 1:
        raise AstridError("scale must be at least 1")
    explicit_block = block is not None
    if block is None:
        lattice = px.detect_square_lattice(source)
        if not lattice["detected"]:
            raise AstridError(
                f"pixel.sprites found no reliable lattice; best hint {lattice['block']:.2f} px/block",
                recovery_command=f"pass block: {lattice['block']:.2f} after checking a source pixel at 8x",
                state_snapshot=lattice,
            )
        block = lattice["block"]
    else:
        lattice = px.detect_square_lattice(source)
    native, _preview, snap_report = px.snap_image(source, block=block, scale=1)
    cutout, cut_report = px.cutout_image(native, mode=mode, key=key, tolerance=tolerance,
                                         holes=holes, keep_largest=False, trim=False)
    regions = px.split_sprites(cutout, cells=cells, merge_distance=merge_distance, padding=padding)
    if not regions:
        raise AstridError("pixel.sprites found no pieces")
    # The sheet grid locates subjects only. Each generated icon can have its
    # own phase or even a slightly different cell size, so snap its source crop
    # independently rather than copying the sheet's sampling phase.
    pieces = []
    rows = []
    for index, (_rough_piece, bbox) in enumerate(regions):
        x0, y0, x1, y1 = bbox
        sx0, sy0 = max(0, round(x0 * block)), max(0, round(y0 * block))
        sx1, sy1 = min(source.shape[1], round(x1 * block)), min(source.shape[0], round(y1 * block))
        crop = source[sy0:sy1, sx0:sx1]
        local = px.detect_square_lattice(crop)
        local_block = float(block if explicit_block or not local["detected"] else local["block"])

        def sample(trial_block):
            snapped, _preview, _snap = px.snap_image(crop, block=trial_block, scale=1)
            return px.cutout_image(snapped, mode=mode, key=key, tolerance=tolerance,
                                   holes=holes, keep_largest=False, trim=True,
                                   trim_padding=padding)

        piece, local_cut = sample(local_block)
        sample_block = local_block
        # A generated edge can drift across the nominal sampling line. Search
        # only a narrow band around the detected density and change it only
        # for a material improvement in intact dark outline boundary cells.
        if not explicit_block:
            baseline = px.outline_integrity(piece)["fraction"]
            best = baseline
            for factor in (0.96, 0.98, 1.02, 1.04):
                candidate_block = local_block * factor
                candidate, candidate_cut = sample(candidate_block)
                score = px.outline_integrity(candidate)["fraction"]
                if score > best:
                    piece, local_cut, sample_block, best = candidate, candidate_cut, candidate_block, score
            if best < baseline + 0.08:
                piece, local_cut = sample(local_block)
                sample_block = local_block
        pieces.append(piece)
        trim = local_cut["trim"]["crop"]
        rows.append({"index": index, "source_bbox": {
            "x": sx0 + round(trim["x"] * sample_block),
            "y": sy0 + round(trim["y"] * sample_block),
            "width": round(piece.shape[1] * sample_block),
            "height": round(piece.shape[0] * sample_block)},
            "drawn_block": local_block,
            "sampling_block": round(sample_block, 3),
            "lattice": local,
            "native": {"width": piece.shape[1], "height": piece.shape[0]},
            "on_screen": {"width": piece.shape[1] * scale, "height": piece.shape[0] * scale},
            "outline_integrity": px.outline_integrity(piece)})
    report = {"source": {"width": source.shape[1], "height": source.shape[0]},
              "grid": snap_report["grid"], "lattice": lattice, "cutout": cut_report,
              "split": {"cells": cells, "merge_distance": merge_distance, "padding": padding},
              "scale": scale, "pieces": rows}
    local_blocks = [row["drawn_block"] for row in rows]
    warnings = []
    if lattice["warning"]:
        warnings.append(lattice["warning"])
    if len(local_blocks) > 1 and max(local_blocks) / min(local_blocks) > 1.07:
        warnings.append("pieces have different drawn densities; at one film scale their native/on-screen sizes differ. Regenerate with a shared target pixel count")
    report["warnings"] = warnings
    return pieces, report


def contact_sheet(pieces, *, scale):
    columns = min(4, len(pieces))
    rows = math.ceil(len(pieces) / columns)
    cell_w = max(piece.shape[1] for piece in pieces) * scale + 24
    cell_h = max(piece.shape[0] for piece in pieces) * scale + 40
    sheet = Image.new("RGBA", (columns * cell_w, rows * cell_h), (247, 244, 237, 255))
    draw = ImageDraw.Draw(sheet)
    for index, piece in enumerate(pieces):
        x, y = (index % columns) * cell_w, (index // columns) * cell_h
        enlarged = Image.fromarray(piece, mode="RGBA").resize(
            (piece.shape[1] * scale, piece.shape[0] * scale), Image.Resampling.NEAREST)
        sheet.alpha_composite(enlarged, (x + 12, y + 28))
        draw.text((x + 12, y + 8), str(index), fill=(37, 36, 31, 255))
    return sheet


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        source = args.image.expanduser().resolve()
        if not source.is_file():
            raise AstridError(f"image not found: {source}")
        out = args.out.expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        pieces, report = make_sprites(
            px.load_rgba(str(source)), mode=args.mode, key=args.key, tolerance=args.tolerance,
            holes=bool(args.holes), block=args.block, cells=args.cells,
            merge_distance=args.merge_distance, padding=args.padding, scale=args.scale)
        # images/ is a namespace the host flattens into one managed row per file.
        sprite_dir = out / "images"
        sprite_dir.mkdir(exist_ok=True)
        for index, piece in enumerate(pieces):
            px.save_rgba(piece, str(sprite_dir / f"sprite-{index:02d}.png"))
        contact_sheet(pieces, scale=args.scale).save(out / "preview.png")
        (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest = build_manifest(
            kind="pixel.sprites",
            inputs={"image": source.name, "mode": args.mode, "key": args.key,
                    "tolerance": args.tolerance, "holes": bool(args.holes),
                    "block": args.block, "cells": args.cells, "merge_distance": args.merge_distance,
                    "padding": args.padding, "scale": args.scale},
            outputs=[
                # One row per piece, each with its ordinal: the host publishes run:<id>/sprites#N
                # from these (a single directory row collapsed every piece onto sprites#0).
                *({"name": "sprites", "path": f"images/sprite-{index:02d}.png", "type": "file",
                   "artifact_type": "image", "ordinal": index, "role": "result", "is_primary": index == 0}
                  for index in range(len(pieces))),
                {"name": "preview", "path": "preview.png", "type": "file", "artifact_type": "image", "role": "result"},
                {"name": "report", "path": "report.json", "type": "file", "role": "auxiliary"},
            ], created=datetime.now(timezone.utc).isoformat())
        write_manifest(out / "manifest.json", manifest)
        return 0
    return run_pack_main("pixel.sprites", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
