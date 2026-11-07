"""Install the pack-lock-selected VibeComfy in the current test interpreter.

Like ``ci_install_runtime.py``, this installs the source archive without
resolving a second dependency set. The Agent's pinned test environment owns
those dependencies.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys


root = pathlib.Path(__file__).resolve().parent.parent
lock = json.loads((root / "astrid" / "packs" / "vibecomfy" / "pins.json").read_text())
source = lock["sources"]["vibecomfy"]
sha = source["revision"]
repository = source["repository"]
subprocess.run(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-deps",
        f"vibecomfy @ git+{repository}@{sha}",
    ],
    check=True,
)
