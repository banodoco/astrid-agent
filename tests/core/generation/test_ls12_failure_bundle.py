"""An in-memory attempt retains partial bytes only in its own producer scope."""
import hashlib
import json

import pytest

from astrid.core.generation.producer_result import (
    preserve_result_on_error,
    retain_workflow_for_failure,
)
from tests.core.generation.test_producer_result_recovery import compiled


def _attempt_bundle(tmp_path):
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    from astrid.packs.vibecomfy.local_server_engine import local_server_bundle

    workflow = VibeWorkflow("failure-scope", WorkflowSource("failure-scope"))
    sink = workflow.add_node("SaveImage", _id="1", uid="save", filename_prefix="result")
    workflow = workflow.finalize({}, output_node=sink, output_type="SaveImage", name="image",
                                 artifact_kind="image", mime_type="image/png", filename_prefix="result")
    parent = emit_bundle(workflow, tmp_path / "source" / "workflow.py", {"operation": "authored"})
    return local_server_bundle(parent, tmp_path / "derived", output_identity="attempt-1")


def _native_failure(destination, bundle, *, wrong_revision=False):
    native = destination / "native"
    native.mkdir(parents=True)
    artifact = native / "result_00001_.png"
    data = b"verified native partial bytes"
    artifact.write_bytes(data)
    record = {**compiled().to_dict(), "revision_id": "other-revision" if wrong_revision else bundle.revision_id}
    receipt = native / "attempt.json"
    receipt.write_text(json.dumps({
        "task_id": "task-1", "attempt_id": "attempt-1",
        "runtime_evidence": {"approved_projection": record, "terminal": {"phase": "verification"}},
        "artifacts": [{"path": str(artifact), "reported_path": artifact.name,
                       "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
    }))
    error = RuntimeError("verification failed after native download")
    error.receipt_path = str(receipt)
    raise error


@pytest.mark.parametrize("mismatch", [None, "task", "attempt", "destination", "revision"])
def test_partial_recovery_requires_the_same_attempt_and_revision(tmp_path, mismatch):
    bundle = _attempt_bundle(tmp_path)
    destination = tmp_path / "out"
    with pytest.raises(RuntimeError, match="verification failed") as caught:
        with preserve_result_on_error(destination, task_id="task-1", attempt_id="attempt-1"):
            retain_workflow_for_failure(
                bundle, tmp_path / "foreign" if mismatch == "destination" else destination,
                task_id="foreign-task" if mismatch == "task" else "task-1",
                attempt_id="foreign-attempt" if mismatch == "attempt" else "attempt-1",
            )
            _native_failure(destination, bundle, wrong_revision=mismatch == "revision")
    payload = json.loads(caught.value.managed_generation_result_path.read_bytes())
    assert payload["outcomes"]["execution"]["status"] == "failed"
    assert payload["outcomes"]["publication"]["status"] == "not_started"
    if mismatch is None:
        assert len(payload["outputs"]) == 1
        output = payload["outputs"][0]
        assert output["hash_check"]["status"] == "verified"
        assert (destination / output["path"]).read_bytes() == b"verified native partial bytes"
    else:
        assert payload["outputs"] == []
        assert payload["warnings"] == ["Native partial artifact has no unambiguous declared output"]


def test_nested_producer_scope_restores_the_outer_attempt(tmp_path):
    bundle = _attempt_bundle(tmp_path)
    destination = tmp_path / "out"
    with pytest.raises(RuntimeError, match="verification failed") as caught:
        with preserve_result_on_error(destination, task_id="task-1", attempt_id="attempt-1"):
            with preserve_result_on_error(tmp_path / "inner", task_id="inner-task", attempt_id="inner-attempt"):
                pass
            retain_workflow_for_failure(bundle, destination, task_id="task-1", attempt_id="attempt-1")
            _native_failure(destination, bundle)
    payload = json.loads(caught.value.managed_generation_result_path.read_bytes())
    assert len(payload["outputs"]) == 1
    assert payload["outputs"][0]["hash_check"]["status"] == "verified"
