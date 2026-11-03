"""Keep hosted journey uploads inside the collector's named evidence boundary."""
from pathlib import Path
import re
import os
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "journey.yml"
COLLECTOR = REPO / "scripts" / "ci_journey.py"


def test_journey_upload_is_allowlisted_staged_evidence_under_size_limit():
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
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
    assert "release0-bootstrap.log" in staged_names
    collector_block = COLLECTOR.read_text(encoding="utf-8").split("JOURNEY_EVIDENCE_NAMES = frozenset({", 1)[1].split("})", 1)[0]
    collector_names = set(re.findall(r'^\s*"([^\"]+)"\s*,?\s*$', collector_block, re.M))
    # Fail closed until upload is bound to validated immutable bytes.
    assert collector_names - staged_names == {"macos-bootstrap-source.json"}
    assert "^runtime-service-service-.+\\.log$" in guard
    assert "Unexpected staged evidence path" in guard
    assert "Unexpected staged evidence filename" in guard
    assert "No staged journey-result.json receipt" in guard
    assert "Get-ChildItem -LiteralPath $phase.FullName -Force" in guard
    assert "-ge $maxArtifactBytes" in guard


@pytest.fixture
def boundary(tmp_path):
    pwsh = os.environ.get("JOURNEY_TEST_PWSH") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell unavailable")
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("      - name: Validate the named lifecycle evidence artifact boundary")
    end = text.index("      - name: Retain only the named lifecycle evidence", start)
    run = text[start:end].split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in run.splitlines())
    script = script.replace("${{ matrix.job_id }}", "test")
    script_path = tmp_path / "guard.ps1"
    script_path.write_text("$ErrorActionPreference = 'Stop'\n" + script, encoding="utf-8")
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), XDG_CACHE_HOME=os.environ.get("XDG_CACHE_HOME", str(tmp_path)))
    root = tmp_path / "astrid-r1-test/evidence-upload"
    phase = root / "before-cleanup"
    phase.mkdir(parents=True)
    (phase / "journey-result.json").write_text("{}")
    def run_guard():
        return subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                              env=env, capture_output=True, text=True, timeout=30)
    return root, phase, run_guard


def test_workflow_boundary_source_record_and_rejections(boundary):
    root, phase, run = boundary
    record = phase / "macos-bootstrap-source.json"
    digest = "a" * 64
    record.write_text(f'{{"status":"observed","candidate_sha256":"{digest}",'
                      f'"installed_sha256":"{digest}","matches":true}}\n')
    # Even the safe record remains rejected until validation and upload are bound.
    assert run().returncode != 0
    for bad in ('{"status":"unavailable","path":"private"}\n', 'x' * 257,
                f'{{"status":"observed","candidate_sha256":"{digest}",'
                f'"installed_sha256":"{digest}","matches":false}}\n'):
        record.write_text(bad)
        assert run().returncode != 0
    record.write_text('{"status":"unavailable"}\n')
    assert run().returncode != 0
    record.unlink()
    for name in ("macos-bootstrap-source.json.bak", "unrelated.txt"):
        item = phase / name
        item.write_text("private marker")
        assert run().returncode != 0
        item.unlink()
    nested = phase / "nested"
    nested.mkdir()
    assert run().returncode != 0


def test_workflow_boundary_symlinks_and_total_limit(boundary, tmp_path):
    root, phase, run = boundary
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "macos-bootstrap-source.json").write_text('{"status":"unavailable"}\n')
    link = phase / "macos-bootstrap-source.json"
    link.symlink_to(outside / link.name)
    assert run().returncode != 0
    link.unlink()
    extra = root / "after-cleanup"
    extra.symlink_to(outside, target_is_directory=True)
    assert run().returncode != 0
    extra.unlink()
    original = root.rename(root.with_name("real-evidence"))
    root.symlink_to(original, target_is_directory=True)
    assert run().returncode != 0
    root.unlink()
    original.rename(root)
    big = phase / "install.log"
    with big.open("wb") as stream:
        stream.truncate(49_000_000 - 2048 - 3)
    assert run().returncode == 0
    with big.open("ab") as stream:
        stream.write(b"x")
    assert run().returncode != 0


def test_workflow_validation_does_not_bind_later_upload_bytes(boundary):
    root, phase, run = boundary
    assert run().returncode == 0
    # The upload action reads the tree in a later step. Demonstrate the gap
    # using a synthetic marker; this is not a test of the uploader itself.
    receipt = phase / "journey-result.json"
    receipt.write_bytes(b"synthetic marker after validation")
    assert (root / "before-cleanup/journey-result.json").read_bytes() == b"synthetic marker after validation"
