"""The source installer manifest is generated from local pins and tool data."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.installer.tools import validate_manifest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.installed
def test_local_manifest_generation_uses_clean_home_and_no_astrid_environment(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    assert list(home.iterdir()) == [], "the local manifest subprocess needs an empty HOME"
    output = tmp_path / "local-install-manifest.json"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ASTRID_", "VIBECOMFY_"))
        and key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/local_source_manifest.py"),
         "--agent-ref", "HEAD", "--output", str(output)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    manifest = json.loads(output.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    assert manifest["agent"]["repo"] == str(ROOT.resolve())
    assert manifest["agent"]["sha"] == subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    assert manifest["runtime"]["sha"] == json.loads(
        (ROOT / "runtime.lock.json").read_text(encoding="utf-8")
    )["source_commit"]
    assert manifest["tools"]["python"]["version"]
