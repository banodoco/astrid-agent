#!/usr/bin/env python3
"""Validate Wan2GP settings without executing the engine."""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("wan2gp.validate_settings")

import argparse
import json
import sys

from astrid.packs.wan2gp.src.compiler import compile_from_inputs, portable_digest
from astrid.packs.wan2gp.src.driver import WAN2GP_PIN_SHA, WAN2GP_PIN_REF, validate_settings as driver_validate


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Validate Wan2GP settings (no engine).")
    p.add_argument("--prompt", required=True)
    p.add_argument("--model", default="wan-2.2")
    p.add_argument("--negative-prompt", dest="negative_prompt", default=None)
    p.add_argument("--resolution", default=None)
    p.add_argument("--frames", type=int, default=None, dest="video_length")
    p.add_argument("--seed", type=int, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inputs = {
        "prompt": args.prompt,
        "model": args.model,
        "negative_prompt": args.negative_prompt,
        "resolution": args.resolution,
        "video_length": args.video_length,
        "seed": args.seed,
    }
    clean = {k: v for k, v in inputs.items() if v is not None}
    try:
        settings = compile_from_inputs(clean)
        driver_validate(settings)
    except ValueError as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    digest = portable_digest(settings)
    json.dump(
        {
            "ok": True,
            "settings": settings,
            "portable_digest": digest,
            "disclosed_engine": {"engine": "wan2gp", "pin_sha": WAN2GP_PIN_SHA, "pin_ref": WAN2GP_PIN_REF},
        },
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
