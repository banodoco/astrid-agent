"""Run Astrid as an executable package."""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("ASTRID_INTERNAL_INVOCATION", "1")

# Capture the launch before importing the gateway, but keep the public module
# callable identical to the gateway callable for embedded users.
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "generate":
    from .console import _STARTUP_MARKS, process_started_at

    _entry = time.monotonic()
    _start = process_started_at(_entry)
    _import_start = time.monotonic()

from .core.gateway import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        _import_end = time.monotonic()
        _provenance = "process start" if _start < _entry else "command entry"
        _token = _STARTUP_MARKS.set((_start, _provenance, _import_start, _import_end))
        try:
            raise SystemExit(main())
        finally:
            _STARTUP_MARKS.reset(_token)
    raise SystemExit(main())
