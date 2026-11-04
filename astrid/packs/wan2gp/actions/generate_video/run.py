#!/usr/bin/env python3
"""Host-mediated Wan2GP capability guard.

The production executor is dispatched by GenericPackHost, which owns the
retained native child.  Direct execution is deliberately disabled so a pack
runner cannot reintroduce per-attempt ``init()``/``close()`` ownership.
"""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("wan2gp.generate_video")

import argparse
import sys
from astrid.packs.wan2gp.src.driver import compile_host_settings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Wan2GP host-mediated native video generation.")
    p.add_argument("--prompt", required=True, help="Text prompt.")
    p.add_argument("--model", default="wan-2.2", help="Model id (default wan-2.2).")
    p.add_argument("--negative-prompt", dest="negative_prompt", default=None, help="Negative prompt.")
    p.add_argument("--resolution", default=None, help="WxH, e.g. 1280x720.")
    p.add_argument("--frames", type=int, default=None, dest="video_length", help="Frames / video_length.")
    p.add_argument("--fps", default=None, help="FPS (stringified to force_fps).")
    p.add_argument("--seed", type=int, default=None, help="Seed.")
    p.add_argument("--guidance-scale", dest="guidance_scale", type=float, default=None)
    p.add_argument("--steps", type=int, default=None, dest="num_inference_steps", help="Sampling steps.")
    p.add_argument("--loras", default=None, help="LoRAs JSON/path (pass-through).")
    return p


def generate_core(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    inputs: dict[str, object] = {
        "prompt": args.prompt,
        "model": args.model,
        "negative_prompt": args.negative_prompt,
        "resolution": args.resolution,
        "video_length": args.video_length,
        "fps": args.fps,
        "seed": args.seed,
        "guidance_scale": args.guidance_scale,
        "steps": args.num_inference_steps,
        "loras": args.loras,
    }
    clean: dict[str, object] = {k: v for k, v in inputs.items() if v is not None}
    try:
        settings = compile_host_settings(clean)
    except ValueError as exc:
        return 2, {"ok": False, "error": str(exc), "code": "invalid_inputs"}
    del settings
    return 2, {
        "ok": False,
        "error": "wan2gp.generate_video requires the GenericPackHost-owned Wan session",
        "code": "host_session_required",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code, payload = generate_core(args)
    # Emit JSON on stdout for host capture; errors also go to stderr.
    import json

    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
