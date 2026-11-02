"""Exercise workflow matrix configuration and Windows evidence upload boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = (REPO / ".github/workflows/journey.yml").read_text(encoding="utf-8")


def config(tmp_path, **overrides):
    script = WORKFLOW.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    output = tmp_path / "output"
    candidate = {name: "a" * 40 for name in ("app", "agent", "runtime", "pinokio")}
    env = dict(os.environ, CANDIDATE=json.dumps(candidate), DRIVER_AGENT_REF="b" * 40,
               WORKFLOW_REPOSITORY="banodoco/astrid-agent",
               OLD_LAUNCHER_REF="bbba620e3d89eeff8021417f365e6ea584203105",
               STOPPED_PATH_AGENT_REF=candidate["agent"], M4_4A_EXECUTOR_CONFIGS='{"linux":{},"macos":{},"windows":{}}',
               DIRECT_ONLY="false", DIRECT_OS="all", M4_4A_BACKEND_ONLY="false", M4_5_ENABLED="false",
               M4_5_ONLY="false", M4_5_BOUNDARY="", M4_5_BOUNDARY_ARGS="[]", M4_4B_ENABLED="false",
               M4_6_ENABLED="false", M4_6_CANDIDATE="", GITHUB_OUTPUT=str(output))
    env.update(overrides)
    # The hosted checkout uses driver/scripts; expose this checkout under that name.
    (tmp_path / "driver").symlink_to(REPO, target_is_directory=True)
    process = subprocess.run([sys.executable, "-c", textwrap.dedent(script)], cwd=tmp_path,
                             env=env, capture_output=True, text=True)
    values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return process, values


def test_default_and_direct_only_matrix(tmp_path):
    process, values = config(tmp_path, DIRECT_OS="")
    assert process.returncode == 0, process.stderr
    rows = json.loads(values["matrix"])["include"]
    assert len(rows) == 9
    assert {row["job_id"] for row in rows if row["mode"] == "direct"} == {
        "direct-linux", "direct-macos", "direct-windows"}
    (tmp_path / "driver").unlink()
    process, values = config(tmp_path, DIRECT_ONLY="true")
    assert process.returncode == 0, process.stderr
    assert {row["job_id"] for row in json.loads(values["matrix"])["include"]} == {
        "direct-linux", "direct-macos", "direct-windows"}


@pytest.mark.parametrize("name", ["linux", "macos", "windows"])
@pytest.mark.parametrize("direct_only", ["true", "false"])
def test_selector_runs_exactly_one_direct_row(tmp_path, name, direct_only):
    process, values = config(tmp_path, DIRECT_OS=name, DIRECT_ONLY=direct_only)
    assert process.returncode == 0, process.stderr
    assert [row["job_id"] for row in json.loads(values["matrix"])["include"]] == ["direct-" + name]


@pytest.mark.parametrize("options", [
    {"DIRECT_OS": "Windows"}, {"DIRECT_OS": "linux,windows"},
    {"DIRECT_OS": "windows", "M4_4A_BACKEND_ONLY": "true"},
    {"DIRECT_OS": "linux", "M4_5_ONLY": "true"},
    {"DIRECT_ONLY": "true", "M4_4A_BACKEND_ONLY": "true"},
])
def test_bad_or_conflicting_selector_is_rejected(tmp_path, options):
    process, _ = config(tmp_path, **options)
    assert process.returncode != 0


def test_windows_evidence_allowlist_and_full_gate():
    block = WORKFLOW.split("      - name: Retain allowlisted Windows direct receipt and bootstrap evidence", 1)[1].split("\n  release2:", 1)[0]
    assert "/astrid-r1-${{ matrix.job_id }}/tst-b1-owner.json" in block
    assert block.count("/evidence/evidence-upload/") == 4
    assert block.count("release0-bootstrap.log") == 2
    assert block.count("receipt.json") == 2
    assert block.count("journey-result.json") == 1
    assert "astrid-r1-${{ matrix.job_id }}/evidence/release0-to-release1/journey-result.json" in block
    assert "merge-multiple: false" in WORKFLOW
    gate = WORKFLOW.split("      - name: Aggregate the exact Release-1 receipt matrix", 1)[1].split("      - name:", 1)[0]
    assert "--aggregate-release1" in gate
    assert "direct_os" not in gate and "direct_only" not in gate
