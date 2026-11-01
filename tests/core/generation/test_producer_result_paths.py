"""Producer evidence must cross the strict harvesting boundary after staging."""
import importlib

import pytest

from astrid.core.generation.producer_result import source_input_ref
from tests.test_producer_evidence_f06 import _managed_payload, _write_production


@pytest.mark.parametrize("workflow_name", ["submitted.json", "workflow.py"])
def test_harvest_retained_source_from_outside_producer_root(tmp_path, workflow_name):
    submitted = tmp_path / "entry"
    submitted.mkdir()
    workflow = submitted / workflow_name
    source = workflow.with_name("source.json") if workflow.suffix == ".py" else workflow
    original = b'{ "prompt": "original source" }\r\n'
    source.write_bytes(original)
    if workflow.suffix == ".py":
        workflow.write_text("# converted workflow; original bytes live in source.json\n")
    producer_root = tmp_path / "producer"
    payload = _managed_payload(producer_root)
    payload["inputs"]["source"] = source_input_ref(workflow, producer_root)
    production = _write_production(producer_root, payload)
    source.unlink()

    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    host_root = tmp_path / "host"
    manifest = run._materialize_managed_generation_result(
        host_root, production, task_identity="task-1", attempt_identity="attempt-1",
        workflow_path=workflow,
    )

    reference = manifest["managed_generation_result"]["inputs"]["source"]
    assert "path" not in reference
    assert reference["sha256"] == payload["inputs"]["source"]["sha256"]
    assert reference["bytes"] == len(original)
    assert reference["filename"] == source.name
    evidence = host_root / "outputs" / f"evidence-source-{source.name}"
    assert evidence.read_bytes() == original
    assert any(row["content_hash"] == reference["object_id"] for row in manifest["outputs"])
