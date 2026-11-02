"""Canonical wrapper for a bounded Generation image profile."""
from __future__ import annotations

import sys

from astrid.pack_api import guard_canonical_entrypoint


def main(argv: list[str] | None = None) -> int:
    guard_canonical_entrypoint("generation.generate_image_edit")
    from astrid.packs.generation.actions.generate_image.run import main as generate_main

    return generate_main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
