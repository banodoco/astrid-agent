"""Keep fresh and embedded OMP-agent generation timing provenance distinct."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(tmp_path: Path, *command: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("ASTRID_CLI_START_MONO", None)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(tmp_path), str(REPO_ROOT), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_omp_agent_module_generate_reports_fresh_process_start(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        sys.executable,
        "-m",
        "astrid.omp_agent",
        "generate",
        "image",
        "--json",
        "--timings",
    )

    assert result.returncode == 2
    assert "prompt is required" in result.stdout
    assert "interpreter start:" in result.stderr
    assert "gateway imports:" in result.stderr
    assert "Traceback" not in result.stderr


def test_embedded_omp_agent_main_generate_uses_command_entry(tmp_path: Path) -> None:
    # Stub the generation facade so the embedded call is deterministic and
    # offline while still passing through omp_agent.main and the gateway.
    (tmp_path / "sitecustomize.py").write_text(
        "from types import SimpleNamespace\n"
        "from astrid.core.generation import cli\n"
        "def image(**kwargs):\n"
        "    return SimpleNamespace(ok=True, to_dict=lambda: {'image_paths': ['embedded-agent.png']})\n"
        "cli._generate_facade = lambda: SimpleNamespace(image=image)\n",
        encoding="utf-8",
    )
    result = _run(
        tmp_path,
        sys.executable,
        "-c",
        "from astrid import omp_agent; raise SystemExit(omp_agent.main(["
        "'generate', 'image', 'a dog', '--dry-run', '--json', '--timings']))",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["image_paths"] == ["embedded-agent.png"]
    assert payload["start"] == "command entry"
    stages = {row["stage"] for row in payload["timings"]}
    assert "command dispatch" in stages
    assert "interpreter start" not in stages
