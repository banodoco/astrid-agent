"""Keep hosted journey uploads inside the collector's named evidence boundary."""
from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "journey.yml"
COLLECTOR = REPO / "scripts" / "ci_journey.py"
BOUNDARY = REPO / "scripts" / "journey_artifact_boundary.py"


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

    assert "python -u agent/scripts/journey_artifact_boundary.py $evidenceRoot" in guard
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in guard
    assert "continue-on-error" not in guard

    collector_block = COLLECTOR.read_text(encoding="utf-8").split("JOURNEY_EVIDENCE_NAMES = frozenset({", 1)[1].split("})", 1)[0]
    collector_names = set(re.findall(r'^\s*"([^\"]+)"\s*,?\s*$', collector_block, re.M))
    boundary_text = BOUNDARY.read_text(encoding="utf-8")
    assert collector_names
    assert "MAX_ARTIFACT_BYTES = 49_000_000" in boundary_text
    assert 'PHASES = frozenset({"before-cleanup", "after-cleanup"})' in boundary_text
    assert "Unexpected staged evidence path" in boundary_text
    assert "Unexpected staged evidence filename" in boundary_text
    assert "No staged journey-result.json receipt" in boundary_text
    assert "minimum footprint cannot fit" in boundary_text
