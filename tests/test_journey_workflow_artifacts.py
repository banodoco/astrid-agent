"""Exercise workflow matrix configuration and Windows evidence upload boundaries."""
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = (REPO / ".github/workflows/journey.yml").read_text(encoding="utf-8")
COLLECTOR = REPO / "scripts" / "ci_journey.py"


def config(tmp_path, **overrides):
    config_step = WORKFLOW.split("      - id: config\n", 1)[1]
    script = config_step.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
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
    guard = WORKFLOW.split("      - name: Validate the named lifecycle evidence artifact boundary", 1)[1].split("      - name: Retain only the named lifecycle evidence", 1)[0]
    assert "'receipt.json'" in guard and "'release0-bootstrap.log'" in guard
    assert "'before-cleanup-result.json'" in guard
    assert "merge-multiple: false" in WORKFLOW
    gate = WORKFLOW.split("      - name: Aggregate the exact Release-1 receipt matrix", 1)[1].split("      - name:", 1)[0]
    assert "--aggregate-release1" in gate
    assert "direct_os" not in gate and "direct_only" not in gate


def test_journey_upload_is_allowlisted_staged_evidence_under_size_limit():
    workflow_text = WORKFLOW
    guard_start = workflow_text.index("      - name: Validate the named lifecycle evidence artifact boundary")
    upload_start = workflow_text.index("      - name: Retain only the named lifecycle evidence", guard_start)
    release2_start = workflow_text.index("  release2:", upload_start)
    guard = workflow_text[guard_start:upload_start]
    upload = workflow_text[upload_start:release2_start]

    assert "id: evidence_boundary" in guard
    assert "if: always()" in guard
    assert "if: always() && steps.evidence_boundary.outcome == 'success'" in upload
    assert "uses: actions/upload-artifact@v4" in upload
    upload_path = re.search(r"^\s+path: (.+)$", upload, re.M).group(1)
    assert upload_path.endswith("/evidence-upload")
    assert upload_path != "${{ runner.temp }}/astrid-r1-${{ matrix.job_id }}"
    assert "compression-level: 0" in upload

    max_bytes = int(re.search(r"\$maxArtifactBytes\s*=\s*(\d+)", guard).group(1))
    assert max_bytes < 50_000_000
    assert re.search(r"\$estimatedArtifactBytes\s+-ge\s+\$maxArtifactBytes", guard)
    assert "[long]$file.Length + 1024" in guard

    names_block = guard.split("$allowedNames = @(", 1)[1].split("\n          )", 1)[0]
    staged_names = set(re.findall(r"'([^']+)'", names_block))
    collector_block = COLLECTOR.read_text(encoding="utf-8").split("JOURNEY_EVIDENCE_NAMES = frozenset({", 1)[1].split("})", 1)[0]
    collector_names = set(re.findall(r'^\s*"([^\"]+)"\s*,?\s*$', collector_block, re.M))
    assert collector_names <= staged_names
    assert "^runtime-service-service-.+\\.log$" in guard
    assert "Unexpected staged evidence path" in guard
    assert "Unexpected staged evidence filename" in guard
    assert "No staged journey-result.json receipt" in guard
    assert "Get-ChildItem -LiteralPath $evidenceRoot -File -Recurse -Force" in guard
    assert "-ge $maxArtifactBytes" in guard
