"""Keep hosted journey uploads inside the collector's named evidence boundary."""
from pathlib import Path
import re


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
    assert "live-seed-failure.json" in staged_names
    assert "live-snapshot-failure.json" in staged_names
    collector_block = COLLECTOR.read_text(encoding="utf-8").split("JOURNEY_EVIDENCE_NAMES = frozenset({", 1)[1].split("})", 1)[0]
    collector_names = set(re.findall(r'"([^\"]+)"', collector_block))
    assert {"runtime-owner-selection.json", "runtime-owner-selection-context.json"} <= collector_names
    assert collector_names <= staged_names
    assert "^runtime-service-service-.+\\.log$" in guard
    assert "Unexpected staged evidence path" in guard
    assert "Unexpected staged evidence filename" in guard
    assert "No staged journey-result.json receipt" in guard
    assert "Get-ChildItem -LiteralPath $evidenceRoot -File -Recurse -Force" in guard
    assert "-ge $maxArtifactBytes" in guard


def test_sdk_capture_upload_requires_closed_receipts():
    text = WORKFLOW.read_text(encoding="utf-8")
    guard = text.split("      - name: Validate the named lifecycle evidence artifact boundary", 1)[1].split(
        "      - name: Retain only the named lifecycle evidence", 1)[0]
    assert "'project-create-capture-status.json', 'sdk-capture-status.json'" in guard
    for required in ("$file.Length -gt 1024", "ConvertFrom-Json", "$keys.Count -ne $expectedKeys.Count",
                     "$keys -cnotcontains $key", "Invalid SDK capture values", "-isnot [bool]"):
        assert required in guard


def test_live_snapshot_failure_upload_uses_producer_schema_and_size_limit():
    text = WORKFLOW.read_text(encoding="utf-8")
    guard = text.split("      - name: Validate the named lifecycle evidence artifact boundary", 1)[1].split(
        "      - name: Retain only the named lifecycle evidence", 1)[0]
    assert "'live-seed-failure.json', 'live-snapshot-failure.json'" in guard
    assert "$name -in @('live-seed-failure.json', 'live-snapshot-failure.json')" in guard
    for required in ("$file.Length -gt 4096", "astrid.live-workspace-failure.v1",
                     "Invalid live workspace failure fields", "Invalid live workspace failure frame",
                     "$allowedRuntimeCodes -cnotcontains $diagnostic.runtime_code",
                     "$allowedFunctions -cnotcontains $frame.function"):
        assert required in guard

    # ConvertFrom-Json can represent integral JSON values as Int64 (PowerShell
    # [long]); accept that alongside Int32 while still rejecting nonintegers.
    assert "($diagnostic.http_status -isnot [int] -and $diagnostic.http_status -isnot [long])" in guard
    assert "($frame.line -isnot [int] -and $frame.line -isnot [long])" in guard
