"""Exercise the actual workflow script with PowerShell, when available."""

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml"


def boundary_script():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    section = workflow.split("      - name: Validate the named lifecycle evidence artifact boundary\n", 1)[1]
    script = section.split("        run: |\n", 1)[1].split("      - name: Retain only the named lifecycle evidence", 1)[0]
    return textwrap.dedent(script).replace("${{ matrix.job_id }}", "direct-windows")


def test_boundary_expressions_are_single_backslashes():
    script = boundary_script()
    assert ".Replace('\\\\', '/')" not in script
    assert ".Replace('\\', '/')" in script
    assert "^runtime-service-service-.+\\.log$" in script
    assert "^runtime-service-service-.+\\\\.log$" not in script


def test_actual_powershell_boundary_accepts_receipt_and_manifest_and_rejects_bad_paths(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell is not installed")
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "share"),
           "POWERSHELL_TELEMETRY_OPTOUT": "1"}
    root = tmp_path / "astrid-r1-direct-windows/evidence-upload/before-cleanup"
    root.mkdir(parents=True)
    (root / "journey-result.json").write_text('{}')
    manifest = root / "runtime-service-service-readiness-1-last-failed-poll.json.log"
    manifest.write_text('{}')
    script = tmp_path / "boundary.ps1"
    script.write_text(boundary_script())
    def run():
        return subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True, env=env)
    result = run()
    assert result.returncode == 0, result.stderr
    assert "Validated named lifecycle artifact" in result.stdout
    bad = root / "runtime-service-service-readiness-1-last-failed-poll.jsonXlog"
    bad.write_text('{}')
    assert run().returncode != 0
    bad.unlink()
    (root / "nested").mkdir()
    (root / "nested/journey-result.json").write_text('{}')
    assert run().returncode != 0
    expression = "if ('before-cleanup\\journey-result.json'.Replace('\\', '/') -cne 'before-cleanup/journey-result.json') { exit 1 }"
    assert subprocess.run([pwsh, "-NoProfile", "-Command", expression], env=env).returncode == 0
