"""Apply FAL's optional image upscaler to a directory of sprite frames."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("fal.sprite_upscale")

from .upscale import DEFAULT_FAL_UPSCALER, ai_upscale_frames_with_fal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--animation", required=True)
    parser.add_argument("--ai-upscale-model", default=DEFAULT_FAL_UPSCALER)
    parser.add_argument("--ai-upscale-factor", type=float, default=2.0)
    parser.add_argument("--ai-upscale-prompt")
    parser.add_argument("--ai-upscale-negative-prompt", default="")
    parser.add_argument("--ai-upscale-creativity", type=float, default=0.35)
    parser.add_argument("--ai-upscale-resemblance", type=float, default=0.8)
    parser.add_argument("--ai-upscale-guidance-scale", type=float, default=4.0)
    parser.add_argument("--ai-upscale-steps", type=int, default=28)
    parser.add_argument("--fal-env-file", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--fal-timeout", type=int, default=900)
    parser.add_argument("--fal-logs", action="store_true")
    transparency = parser.add_mutually_exclusive_group()
    transparency.add_argument("--transparent", dest="transparent", action="store_true")
    transparency.add_argument("--no-transparent", dest="transparent", action="store_false")
    parser.set_defaults(transparent=True)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.frames_dir.is_dir():
        parser_error("--frames-dir must name an existing directory")
    frames = sorted(args.frames_dir.glob("*.png"))
    if not frames:
        parser_error("--frames-dir contains no PNG frames")
    if args.ai_upscale_factor <= 0:
        parser_error("--ai-upscale-factor must be > 0")
    outputs, reports = ai_upscale_frames_with_fal(
        [str(path) for path in frames], args.out_dir, args=args, force=args.force
    )
    print(json.dumps({"outputs": outputs, "report": reports}, indent=2))
    return 0


def parser_error(message: str) -> None:
    raise SystemExit(message)


if __name__ == "__main__":
    run_pack_main(main)
