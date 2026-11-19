"""LS-12: an attempt prefix derives from admitted IR without re-emission."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from astrid.packs.vibecomfy import local_server_engine as engine


def _source():
    from vibecomfy.registry.ready import repo_ready_template_root
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle

    return load_bundle(repo_ready_template_root() / "image/qwen_image_2_1_turbo.py",
                       trust=Provenance.USER_CONFIRMED)


def test_private_prefix_derives_once_from_admitted_ir_without_emission(tmp_path, monkeypatch):
    from vibecomfy import workflow_bundle as bundles

    original = _source()
    source_bytes = original.python_path.read_bytes()
    original_projection = deepcopy(original.workflow.semantic_projection())
    presentation = deepcopy(original.ui_sidecar["presentation"])
    calls = []
    emit = bundles.emit_bundle_with_candidate

    def observe(*args, **kwargs):
        calls.append(None)
        return emit(*args, **kwargs)

    monkeypatch.setattr(bundles, "emit_bundle_with_candidate", observe)
    children = [engine.local_server_bundle(original, tmp_path / str(index),
                 output_identity=f"attempt-{index}") for index in range(2)]
    assert calls == []
    assert original.workflow.semantic_projection() == original_projection
    assert original.python_path.read_bytes() == source_bytes
    for index, child in enumerate(children):
        child.require_canonical_authority("LS-12 attempt")
        assert child.parent_revision == original.revision_id
        assert child.revision_id != original.revision_id
        assert child.workflow is not original.workflow
        assert child.ui_sidecar["presentation"] == presentation
        assert bundles._rebind_current_bundle(child).revision_id == child.revision_id
        retained = Path(child.workflow.source.path)
        assert retained.read_bytes() == source_bytes
        assert json.loads(bundles._sidecar_path(retained).read_bytes()) == original.ui_sidecar
        for output in child.workflow.outputs:
            node = child.workflow.nodes[output.node_id]
            prefix = node.inputs.get("filename_prefix", node.widgets.get("filename_prefix"))
            assert prefix.endswith(f"_astrid_attempt-{index}")
    assert children[0].revision_id != children[1].revision_id


def test_changed_parent_is_refused_before_attempt_derivation(tmp_path):
    from vibecomfy.workflow_bundle import WorkflowBundleError

    original = _source()
    original.workflow.set_input("prompt", "changed after admission")
    with pytest.raises(WorkflowBundleError, match="stale"):
        engine.local_server_bundle(original, tmp_path, output_identity="attempt-1")


def _target(bundle):
    """Real packaged schemas with exactly the stand-in's declared model menus."""
    from astrid.core.generation.target_inventory import declared_rows
    from vibecomfy.model_assets import _referenced_model_values
    from vibecomfy.porting.object_info import get_class
    from vibecomfy.schema.provider import TargetSchemaProvider

    info = {}
    for node in bundle.workflow.runtime_nodes().values():
        row = get_class(node.class_type)
        assert row is not None
        info[node.class_type] = {
            "input": deepcopy(row["inputs"]), "input_order": deepcopy(row["input_order"]),
            "output": [item["type"] for item in row["outputs"]],
            "output_name": [item["name"] for item in row["outputs"]],
            "output_is_list": [item["is_list"] for item in row["outputs"]],
        }
    for reference in _referenced_model_values(bundle.workflow):
        groups = info[reference["class_type"]]["input"]
        for group in ("required", "optional"):
            if reference["field"] in groups.get(group, {}):
                groups[group][reference["field"]] = [[reference["value"]]]
    return TargetSchemaProvider.from_object_info(
        info, server_url="http://127.0.0.1:8188", model_availability="target",
        auxiliary_model_custody="target", target_model_assets=declared_rows(bundle.workflow),
        refresh_object_info=lambda: info,
    ), info


def test_attempt_still_compiles_and_rejects_a_digest_consistent_forged_record(tmp_path, monkeypatch):
    from vibecomfy.testing.canonical import canonical_digest
    from vibecomfy.workflow import VibeWorkflow
    from vibecomfy.workflow_bundle import ApprovedProjectionRecord, WorkflowBundleError

    child = engine.local_server_bundle(_source(), tmp_path, output_identity="attempt-1")
    provider, _ = _target(child)
    calls = []
    compile_workflow = VibeWorkflow.compile

    def observe(self, *args, **kwargs):
        calls.append(None)
        return compile_workflow(self, *args, **kwargs)

    monkeypatch.setattr(VibeWorkflow, "compile", observe)
    record = engine._approve_live(child, {}, provider)
    assert calls
    calls.clear()
    record.assert_matches(child, run_inputs={}, api_projection=record.api_projection,
                          ui_projection=record.ui_projection)
    assert calls, "ordinary queue verification must recompile the current candidate"
    forged = record.to_dict()
    sink = next(iter(child.workflow.outputs)).node_id
    forged["api_projection"][str(sink)]["inputs"]["filename_prefix"] = "another-attempt"
    forged["api_digest"] = canonical_digest(forged["api_projection"])
    counterfeit = ApprovedProjectionRecord.from_dict(forged)
    with pytest.raises(WorkflowBundleError, match="does not match current bundle"):
        counterfeit.assert_matches(child, run_inputs={}, api_projection=counterfeit.api_projection,
                                   ui_projection=counterfeit.ui_projection)


def test_attempt_still_requires_current_target_classes(tmp_path):
    from vibecomfy.schema.provider import TargetSchemaProvider
    from vibecomfy.workflow_bundle import WorkflowBundleError

    child = engine.local_server_bundle(_source(), tmp_path, output_identity="attempt-1")
    _, info = _target(child)
    del info["SaveImage"]
    provider = TargetSchemaProvider.from_object_info(
        info, server_url="http://127.0.0.1:8188", model_availability="target",
    )
    with pytest.raises(WorkflowBundleError, match="Unknown class_type SaveImage"):
        engine._approve_live(child, {}, provider)
