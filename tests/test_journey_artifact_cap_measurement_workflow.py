"""Keep the hosted R-01 cap measurement isolated from lifecycle qualification."""
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "journey-artifact-cap-measurement.yml"


def test_artifact_cap_measurement_is_lane_push_only_and_uploads_the_bounded_fixture():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" not in workflow
    assert "push:" in workflow
    assert "lane/M-QUAL/R-01-HOSTED-CAP-MEASUREMENT-R2" in workflow
    assert "runs-on: windows-2022" in workflow
    assert "journey_artifact_cap_measurement_fixture.py" in workflow
    assert "journey_artifact_boundary.py" in workflow
    assert "ref: ${{ github.workflow_sha }}" in workflow
    assert "id: evidence_boundary" in workflow
    assert "if: always() && steps.evidence_boundary.outcome == 'success'" in workflow
    assert "name: r01-artifact-cap-measurement-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "r01-artifact-cap-measurement/evidence-upload" in workflow
    assert "compression-level: 0" in workflow
