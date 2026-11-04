"""The VibeComfy revision is pinned once; no other tracked file may copy it."""

from __future__ import annotations

import subprocess
from pathlib import Path

from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
from scripts import vibecomfy_pin

ROOT = Path(__file__).resolve().parents[1]


def test_pin_script_reads_the_constant() -> None:
    assert vibecomfy_pin.read() == VIBECOMFY_ENGINE_REVISION


def test_no_other_tracked_file_copies_the_pin() -> None:
    result = subprocess.run(
        ["git", "grep", "-l", "-I", VIBECOMFY_ENGINE_REVISION],
        cwd=ROOT, capture_output=True, text=True,
    )
    # git grep exits 1 for "no matches"; anything else (e.g. 128 outside a repo) is a failure.
    assert result.returncode in (0, 1), result.stderr
    tracked = result.stdout.split()
    # Historical release metadata must retain the reviewed default used to
    # reconstruct its installer-written manifest. It is the sole intentional
    # second inventory entry; all other tracked copies remain forbidden.
    allowed = {
        "astrid/core/generation/vibecomfy_dependency.py",
        "tests/upgrade/releases.json",
    }
    assert set(tracked) <= allowed, f"derive these from vibecomfy_dependency.py instead: {sorted(set(tracked) - allowed)}"
