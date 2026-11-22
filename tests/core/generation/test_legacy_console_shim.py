"""Keep the pre-A2 ``astrid.console:main`` executable shim behavior covered."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
OLD_SHIM = "from astrid.console import main; raise SystemExit(main())"


def _run_old_shim(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("ASTRID_CLI_START_MONO", None)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(tmp_path), str(REPO_ROOT), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [sys.executable, "-c", OLD_SHIM, *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_old_console_main_shim_generate_dry_run_uses_command_entry(tmp_path: Path) -> None:
    # Keep generation offline and deterministic while exercising a fresh process
    # through the exact entry callable used by the pre-A2 console script.
    (tmp_path / "sitecustomize.py").write_text(
        "from types import SimpleNamespace\n"
        "from astrid.core.generation import cli\n"
        "def image(**kwargs):\n"
        "    return SimpleNamespace(ok=True, to_dict=lambda: {'image_paths': ['legacy-shim.png']})\n"
        "cli._generate_facade = lambda: SimpleNamespace(image=image)\n",
        encoding="utf-8",
    )

    result = _run_old_shim(
        tmp_path, "generate", "image", "a dog", "--dry-run", "--json", "--timings"
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["image_paths"] == ["legacy-shim.png"]
    assert payload["start"] == "command entry"
    stages = {row["stage"] for row in payload["timings"]}
    assert {"gateway imports", "command dispatch"} <= stages
    assert "interpreter start" not in stages


def test_old_console_main_shim_generate_help_is_preserved(tmp_path: Path) -> None:
    result = _run_old_shim(tmp_path, "generate", "--help")

    assert result.returncode == 0, result.stderr
    assert "usage: astrid generate" in result.stdout
    assert "Generate media through the selected Comfy target" in result.stdout
    assert "image" in result.stdout
    assert "Traceback" not in result.stderr
