"""Regression proof for the shared provider-rate unit-test boundary."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_new_neighboring_test_cannot_swallow_provider_rate_attempt():
    """A newly named unit test still fails if it swallows the API sentinel."""
    package_dir = Path(__file__).parent
    probe = package_dir / "test_rate_boundary_probe_new_neighbor.py"
    probe.write_text(
        """def test_new_neighbor():
    from astrid.packs.runpod.shared.common import _get_hourly_rate
    try:
        rate = _get_hourly_rate("local-test-key", "unlisted-test-gpu")
    except Exception:
        pass
    else:
        assert rate == 0.50
""",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(probe)],
            cwd=package_dir.parents[2],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    finally:
        probe.unlink(missing_ok=True)

    assert result.returncode != 0, result.stdout
    assert "attempted 1 provider GPU-rate lookup(s)" in result.stdout
