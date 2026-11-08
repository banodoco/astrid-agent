"""The core/pack boundary ratchet is enforced by the suite, not only by a manual run."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_core_pack_boundary_ratchet_holds() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_core_pack_boundary.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
