#!/usr/bin/env python3
"""Unified dispatcher for Astrid understanding executors.


`understanding.understand` selects an underlying modality executor (audio, visual,
or video) via `--mode` and forwards the remaining argv unchanged. This is
deliberately a thin executor — not an orchestrator — because it wraps exactly
one executor call with a switch.
"""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('understanding.understand')
import argparse
import sys
from collections.abc import Callable
from importlib import import_module

from astrid.pack_api import add_choice_arg

ALIASES: dict[str, str | Callable[[list[str]], int]] = {
    "audio": "astrid.packs.understanding.actions.audio_understand.run:main",
    "image": "astrid.packs.understanding.actions.visual_understand.run:main",
    "visual": "astrid.packs.understanding.actions.visual_understand.run:main",
    "video": "astrid.packs.understanding.actions.video_understand.run:main",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dispatch to audio, image/visual, or video understanding executors.",
        epilog=(
            "Examples: understand --mode image --image frame.jpg --query 'What is here?'; "
            "understand --mode audio --audio quote.wav; "
            "understand --mode video --video source.mp4 --at 01:20"
        ),
    )
    add_choice_arg(
        parser,
        "--mode",
        values=sorted(ALIASES),
        required=True,
        help="Understanding modality. All other arguments are forwarded to the selected executor.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args, forwarded = build_parser().parse_known_args(argv)
    target = ALIASES[args.mode]
    if callable(target):
        return target(forwarded)
    module_name, function_name = target.split(":", 1)
    return getattr(import_module(module_name), function_name)(forwarded)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
