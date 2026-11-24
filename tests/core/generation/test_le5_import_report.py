"""LE-5 producer evidence: real canonical import, explicit report custody."""
from astrid.packs.vibecomfy.production_engine import load_workflow_path
from tests.core.generation.fixtures.plan56_sam import (  # noqa: F401
    sam_api_path, sam_offline_lookup,
)


def test_json_loader_returns_import_report_without_mutating_sealed_revision(tmp_path, sam_api_path):
    loaded = load_workflow_path(sam_api_path, tmp_path / "canonical")
    report = loaded.import_report
    assert report is not None
    assert report["revision_id"] == loaded.resolved.revision_id
    assert isinstance(report["provenance"]["source_provenance"]["records"], list)
    assert {"SAMAudioPipelineLoader", "SAMAudioTextSeparator"} <= {
        row.get("class_type") for row in report["diagnostics"]
        if row["code"] in {"unresolved_runtime_class", "unknown_class_type"}
    }
    assert "_astrid_import_report" not in loaded.resolved.workflow.metadata
