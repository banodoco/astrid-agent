"""The tracked parent's real manifest remains harvestable after final publication."""
import json
from types import SimpleNamespace

from astrid.core._shared.result_manifest import harvest_staged_outputs
from astrid.packs.h3_av.actions.transform import run as transform
from astrid.packs.h3_av.shared.input_bundle import build_input_bundle
from astrid.sdk import _child_bridge


def test_parent_candidate_and_receipt_have_distinct_manifest_ordinals(tmp_path, monkeypatch):
    request = {
        "version": 1, "operation": "continue", "source": {"asset": "source", "range": [0, 1]},
        "output": {"duration": 2}, "content": {"prompt": "continue"},
        "changes": {"video": [{"during": [1, 2], "area": {"full_frame": True}, "action": "generate"}],
                    "audio": [{"during": [1, 2], "action": "generate"}]},
        "references": [], "overrides": {},
    }
    normalized = transform.normalize_request(request)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"managed-source")
    bundle = build_input_bundle(normalized, {"source": str(source)}, tmp_path / "inputs.zip")
    roots = {name: {"object_id": transform._parent_digest(path.read_bytes())}
             for name, path in (("request", request_path), ("input_bundle", bundle), ("source", source))}
    out = tmp_path / "parent"
    out.mkdir()
    candidate = tmp_path / "published.media"
    candidate.write_bytes(b"published-candidate")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "overall_status": "candidate_verified", "states": {
            name: {"status": "passed"} for name in (
                "raw_managed_publication", "candidate_verified", "final_composition_publication")},
    }))

    def completed_workflow(args):
        args._managed_client.results["finalizer"] = SimpleNamespace(outputs={"managed_outputs": [{
            "output_port": "verified_candidate", "object_id": transform._parent_digest(candidate.read_bytes()),
            "media_type": "video/mp4",
        }]})
        return {"verified_candidate": str(candidate), "final_receipt": str(receipt)}

    monkeypatch.setenv(transform.ASTRID_TASK_RUN_ID, "fixture-parent")
    monkeypatch.setattr(_child_bridge, "_bridge", object())
    monkeypatch.setattr(transform, "run_transform", completed_workflow)
    result = transform.execute(
        request=request_path, input_bundle=bundle, source=source, host_output_root=out,
        plan={"version": 1, "project": "fixture", "roots": roots, "request_digest": normalized.digest},
    )
    assert result["status"] == "candidate_verified"
    outputs = harvest_staged_outputs(out, require=True)
    assert {row["name"]: row["ordinal"] for row in outputs} == {"verified_candidate": 0, "final_receipt": 1}
