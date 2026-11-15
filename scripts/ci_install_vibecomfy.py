"""Install the pack-lock-selected VibeComfy in the current test interpreter.

Like ``ci_install_runtime.py``, this installs the source archive without
resolving a second dependency set. The Agent's pinned test environment owns
those dependencies.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys


root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

from astrid.core.generation.engine_recipe import load_recipe  # noqa: E402

# The digest-verified recipe is the one source pin, as in CI and vibecomfy_pin.py.
source = next(item for item in load_recipe("comfyui", "windows-x86_64-cu130").sources
              if item.name == "vibecomfy")
sha = source.pin.revision
repository = source.pin.repository
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
