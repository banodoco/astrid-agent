"""Verify that service startup is observed before importing host dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MARKER = "review: assets import reached"


def test_service_reports_import_start_before_rendering_assets(tmp_path):
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\n"
        "class InterruptImport:\n"
        "    def find_spec(self, fullname, *_):\n"
        "        if fullname == 'astrid.core.rendering.assets':\n"
        "            print('review: assets import reached', file=sys.stderr, flush=True)\n"
        "            raise RuntimeError('review import fault')\n"
        "sys.meta_path.insert(0, InterruptImport())\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(tmp_path), str(ROOT), env.get("PYTHONPATH", "")))
    )
    result = subprocess.run(
        [sys.executable, "-m", "astrid.core.execution.generic_host", "--service", "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0, result.stderr
    assert MARKER in result.stderr, result.stderr
    assert "review import fault" in result.stderr, result.stderr
    before_marker = result.stderr.split(MARKER, 1)[0]
    rows = [json.loads(line) for line in before_marker.splitlines() if line.startswith("{")]
    assert any(
        row.get("event") == "astrid.host_startup_timing"
        and row.get("phase") == "module_import_begin"
        for row in rows
    ), result.stderr
