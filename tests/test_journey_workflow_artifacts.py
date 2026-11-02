"""Keep hosted journey uploads inside the collector's named evidence boundary."""
import re
from pathlib import Path

from scripts import journey_artifact_boundary as boundary


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "journey.yml"


def test_journey_upload_is_allowlisted_staged_evidence_under_size_limit():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    guard = workflow.split("      - name: Validate the named lifecycle evidence artifact boundary", 1)[1].split(
        "      - name: Retain only the named lifecycle evidence", 1)[0]
    upload = workflow.split("      - name: Retain only the named lifecycle evidence", 1)[1].split("  release2:", 1)[0]

    assert "id: evidence_boundary" in guard
    assert "if: always()" in guard
    assert "agent/scripts/journey_artifact_boundary.py $evidenceRoot" in guard
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in guard
    assert "if: always() && steps.evidence_boundary.outcome == 'success'" in upload
    assert "uses: actions/upload-artifact@v4" in upload
    upload_path = re.search(r"^\s+path: (.+)$", upload, re.M).group(1)
    assert upload_path.endswith("/evidence-upload")
    assert upload_path != "${{ runner.temp }}/astrid-r1-${{ matrix.job_id }}"
    assert "compression-level: 0" in upload

    assert boundary.MAX_ARTIFACT_BYTES == 49_000_000
