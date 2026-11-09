#!/usr/bin/env python3
"""Re-export of ``astrid.packs.runpod.storage_probe`` (moved in Plan 32 T5).

The pod runs the pack module's own source; this file keeps the old import path
and command line for ``scripts/pod_session.py`` and its tests.
"""
from __future__ import annotations

import os  # noqa: F401  (tests patch probe.os)
import sys
from pathlib import Path

try:
    from astrid.packs.runpod.storage_probe import (  # noqa: F401
        DIAGNOSTIC_PREFIX, DIAGNOSTIC_SCHEMA, main, storage_probe, strict_json_object,
        validate_provider_mapping, _unescape,
    )
except ModuleNotFoundError:  # run as a bare script from a checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from astrid.packs.runpod.storage_probe import (  # noqa: F401
        DIAGNOSTIC_PREFIX, DIAGNOSTIC_SCHEMA, main, storage_probe, strict_json_object,
        validate_provider_mapping, _unescape,
    )

SOURCE_PATH = Path(__file__).resolve().parents[1] / "astrid" / "packs" / "runpod" / "storage_probe.py"


if __name__ == "__main__":
    sys.exit(main())
