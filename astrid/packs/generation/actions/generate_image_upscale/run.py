"""Dedicated bounded SeedVR2 upscale route.

The implementation deliberately reuses the reviewed generation backend and
manifest construction, while this entrypoint owns a narrower fixed request
scope and storage envelope than the broad image generator.
"""

from __future__ import annotations

import sys

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("generation.generate_image_upscale")


def run_sdk(argv: list[str] | None = None) -> dict[str, object]:
    from astrid.packs.generation.actions.generate_image.run import run_sdk as run_image_sdk

    return run_image_sdk(argv)


def main(argv: list[str] | None = None) -> int:
    result = run_sdk(list(sys.argv[1:] if argv is None else argv))
    if result.get("returncode") != 0:
        error = result.get("error")
        if isinstance(error, dict):
            raise AstridError(str(error.get("cause") or "bounded image upscale failed"))
        raise AstridError("bounded image upscale failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
