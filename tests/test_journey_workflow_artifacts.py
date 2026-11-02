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
    collector_block = COLLECTOR.read_text(encoding="utf-8").split("JOURNEY_EVIDENCE_NAMES = frozenset({", 1)[1].split("})", 1)[0]
    collector_names = set(re.findall(r'^\s*"([^\"]+)"\s*,?\s*$', collector_block, re.M))
    assert collector_names <= staged_names
    assert "^runtime-service-service-.+\\.log$" in guard
    assert "Unexpected staged evidence path" in guard
    assert "Unexpected staged evidence filename" in guard
    assert "No staged journey-result.json receipt" in guard
    assert "Get-ChildItem -LiteralPath $evidenceRoot -File -Recurse -Force" in guard
    assert "-ge $maxArtifactBytes" in guard
