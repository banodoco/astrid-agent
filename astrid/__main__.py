"""Run Astrid as an executable package."""

from __future__ import annotations

import os

os.environ.setdefault("ASTRID_INTERNAL_INVOCATION", "1")

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        from .console import entrypoint as main
    else:
        from .core.gateway import main
    raise SystemExit(main())
