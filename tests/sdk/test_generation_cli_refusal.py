"""Public generation CLI behavior when a managed target is unavailable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_managed_image_dry_run_without_runtime_returns_clean_json_refusal(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("ASTRID_", "VIBECOMFY_", "XDG_"))}
    env.update({
        "HOME": str(home),
        "ASTRID_HOME": str(home / ".astrid"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": os.environ.get("PYTHONPATH", str(repo)),
    })
    invocation = (
        "from astrid.console import main; "
        "raise SystemExit(main(['generate', 'image', 'a red boat', '--entry', 'z-image', "
        "'--mode', 't2i', '--placement', 'managed', '--dry-run', '--json']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", invocation],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["issues"][0]["code"] == "engine_not_found"
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr

