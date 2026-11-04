#!/usr/bin/env python3
"""Print the VibeComfy revision Astrid is pinned to.

The pin authority is the packaged VibeComfy engine recipe. The dependency
adapter, CI and this command all read its digest-verified source pin.

    python scripts/vibecomfy_pin.py            # print the pinned revision

Direct SHA bumps are unsupported. A revision change requires coordinated
repinning of the package-owned pin manifests, source node lock and dependent
recipe, environment and index digests. Run the VibeComfy-related tests against
a checkout at the new revision after that update.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    # Keep the documented checkout command usable without an editable install.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrid.core.generation.engine_recipe import engine_source_revision


def read() -> str:
    return engine_source_revision("vibecomfy", "windows-x86_64-cu130", "vibecomfy")


def bump(revision: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SystemExit("expected a full 40-character lowercase commit sha")
    raise SystemExit(
        "direct SHA bumps are unsupported; coordinate the packaged pins, "
        "source node lock and dependent recipe, environment and index digests"
    )


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        raise SystemExit(__doc__)
    if argv:
        bump(argv[0])
    print(read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
