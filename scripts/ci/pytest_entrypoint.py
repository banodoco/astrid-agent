"""Import the installed pytest in isolation before restoring project paths.

Invoke this absolute file with python -I -u. Candidate conftests and plugins
still run, but cannot replace the pytest command before argument capture.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def main():
    root = str(Path(__file__).resolve().parents[2])
    paths = [root]
    if "PYTHONPATH" in os.environ:
        paths.extend(os.environ["PYTHONPATH"].split(os.pathsep))
    sys.path[:0] = paths
    return pytest.console_main()


if __name__ == "__main__":
    raise SystemExit(main())
