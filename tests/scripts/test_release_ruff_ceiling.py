"""The authorized #742 import repair stays below the existing 2008 ceiling."""

import json
import subprocess
import sys
from pathlib import Path


def test_release_ruff_findings_do_not_exceed_2008():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", ".", "--no-cache", "--output-format=json"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode in (0, 1), result.stdout + result.stderr
    findings = json.loads(result.stdout)
    assert len(findings) <= 2008, f"Ruff findings: {len(findings)} exceeds unchanged ceiling 2008"
