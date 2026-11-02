#!/usr/bin/env python3
"""Cut a background out of a managed image into hard-alpha RGBA (pixel.cutout).

Invoked by the Astrid runtime from the pack's V3 action declaration. The algorithm
lives in ``astrid.packs.pixel.actions._common``.
"""

from __future__ import annotations

from astrid.pack_api import add_choice_arg
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("pixel.cutout")

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import AstridError
from astrid.packs.pixel.actions import _common as px




def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pixel.cutout",
        description="Remove a background into hard-alpha RGBA.",
    )
    parser.add_argument("--image", type=Path, required=True, help="Source image (managed path).")
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    parser.add_argument("--crop", default=None, help="Optional {x,y,width,height} in source pixels.")
    add_choice_arg(parser, "--mode", values=("chroma", "flat", "luma"), default="chroma")
    parser.add_argument("--key", default="#FF00FF", help="Chroma key colour (hex).")
    parser.add_argument("--tolerance", type=int, default=48, help="RGB distance counted as background.")
    parser.add_argument("--grid", default=None, help="'auto' or WIDTHxHEIGHT to snap before cutting.")
    add_choice_arg(parser, "--fit", values=("cover", "contain", "none"), default="contain")
    parser.add_argument("--trim", type=px.parse_bool, default=True)
    parser.add_argument("--trim-padding", type=int, default=2)
    parser.add_argument("--keep-largest", type=px.parse_bool, default=True)
    parser.add_argument("--holes", type=px.parse_bool, default=False, help="Also remove key-coloured pixels enclosed by the sprite.")
    parser.add_argument("--allow-lossy", type=px.parse_bool, default=False, help="Permit a coarse explicit grid.")
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        source = args.image.expanduser().resolve()
        if not source.is_file():
            raise AstridError(
                f"image not found: {source}",
                recovery_command="import the image with python -m astrid media import and pass its managed digest",
            )
        out_dir = args.out.expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        crop = px.parse_crop(args.crop)
        source_arr = px.load_rgba(str(source))
        arr = px.apply_crop(source_arr, crop)
        rgba, report = px.cutout_image(
            arr,
            mode=args.mode,
            key=args.key,
            tolerance=int(args.tolerance),
            grid=args.grid,
            fit=args.fit,
            trim=bool(args.trim),
            trim_padding=int(args.trim_padding),
            keep_largest=bool(args.keep_largest),
            holes=bool(args.holes),
            allow_lossy=bool(args.allow_lossy),
        )
        report["crop"] = crop
        report["source_size"] = {"width": int(source_arr.shape[1]), "height": int(source_arr.shape[0])}

        px.save_rgba(rgba, str(out_dir / "cutout.png"))
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        manifest = build_manifest(
            kind="pixel.cutout",
            inputs={
                "image": source.name,
                "crop": crop,
                "mode": args.mode,
                "key": args.key,
                "tolerance": int(args.tolerance),
                "grid": args.grid,
                "fit": args.fit,
                "trim": bool(args.trim),
                "keep_largest": bool(args.keep_largest),
                "holes": bool(args.holes),
                "allow_lossy": bool(args.allow_lossy),
            },
            outputs=[
                {"name": "cutout", "path": "cutout.png", "type": "file", "artifact_type": "image", "role": "result", "is_primary": True},
                {"name": "report", "path": "report.json", "type": "file", "role": "auxiliary"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(out_dir / "manifest.json", manifest)
        return 0

    return run_pack_main("pixel.cutout", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
