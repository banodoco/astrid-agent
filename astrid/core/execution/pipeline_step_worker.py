"""Process-isolated callback worker for pipeline steps."""

from __future__ import annotations

import importlib
import pickle
import sys
import traceback
from pathlib import Path


def run(request_path: str | Path) -> int:
    payload = pickle.loads(Path(request_path).read_bytes())
    module = importlib.import_module(str(payload["module"]))
    callback = getattr(module, str(payload["function"]))
    result = callback(payload["args"])
    if result is not None:
        print(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run(sys.argv[1]))
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
