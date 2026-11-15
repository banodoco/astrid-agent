"""Generation progress for ``ASTRID_PROGRESS_PATH`` (Plan 32 T4g).

A local generation run writes ``{"phase", "percent"}`` to the file the caller
names; on a rented machine that file is the one ``machine.poll`` copies back to
the controller. Without the variable (direct CLI use) every call is a no-op.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PROGRESS_PATH_ENV = "ASTRID_PROGRESS_PATH"
#: The phases a local generation run reports, in order.
PHASES = (("prepare", 5), ("align", 15), ("start_engine", 30), ("run", 50), ("finalize", 90), ("done", 100))
_PERCENT = dict(PHASES)


def write_progress(phase: str, percent: int | None = None, **extra: int) -> bool:
    """Atomically publish the newest phase; never lets a progress failure fail the run."""
    raw = os.environ.get(PROGRESS_PATH_ENV)
    if not raw:
        return False
    payload = {"phase": phase, "percent": _PERCENT.get(phase) if percent is None else percent, **extra}
    path = Path(raw)
    try:
        partial = path.with_name(path.name + ".partial")
        partial.write_text(json.dumps({k: v for k, v in payload.items() if v is not None}, sort_keys=True),
                           encoding="utf-8")
        os.replace(partial, path)
    except OSError:
        return False
    return True
