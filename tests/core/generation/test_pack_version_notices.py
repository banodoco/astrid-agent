"""LE-10 renderer witnesses; target_schema integration follows WS-0g."""
from astrid.core.generation.entries import pack_version_notices
from astrid.core.generation.preflight import readiness_envelope
from vibecomfy.porting.provenance import extract_provenance


REPO = "https://github.com/example/fixture-pack"
FIRST = "a" * 40
SECOND = "b" * 40


def _receipt(version):
    return extract_provenance({"nodes": [
        {"id": 1, "type": "FixtureNode",
         "properties": {"cnr_id": "fixture-pack", "ver": version}},
    ]}).to_json()


def _selected(**fields):
    return [{"slug": "fixture-pack", "source": "git", "url": REPO,
             "commit": SECOND, **fields}]


def test_ui_version_and_selected_commit_are_both_visible():
    notice, = pack_version_notices(_receipt("1.0.0"), _selected(version="2.0.0"), installing=True)
    assert notice["message"] == f"fixture-pack: recorded 1.0.0, installing 2.0.0 ({SECOND})"
    assert notice["details"]["recorded_versions"] == ["1.0.0"]
    assert notice["details"]["selected_commit"] == SECOND
    envelope = readiness_envelope(issues=[notice["issue"]])
    assert envelope["status"] == "ready_with_warnings"
    assert envelope["issues"][0]["code"] == "pack_version_differs"
    assert envelope["fixes"] == []


def test_exact_different_commits_warn_but_selection_does_not_claim_install():
    notice, = pack_version_notices(_receipt(FIRST), _selected(), installing=False)
    assert notice["message"] == f"fixture-pack: recorded {FIRST}, selected {SECOND}"
    assert notice["issue"]["code"] == "pack_version_differs"


def test_explicit_api_pin_is_not_a_claim_of_recorded_version():
    receipt = extract_provenance({"1": {"class_type": "FixtureNode", "inputs": {}}}).to_json()
    notice, = pack_version_notices(receipt, _selected(entry_add=True), installing=True)
    assert notice["message"] == f"fixture-pack: version not recorded; pinned at {SECOND} by you"
    assert notice["issue"] is None


def test_incomparable_label_does_not_manufacture_a_mismatch():
    notice, = pack_version_notices(_receipt("1.0.0"), _selected(), installing=True)
    assert notice["message"] == (
        f"fixture-pack: recorded 1.0.0, installing {SECOND}; version correspondence is unverified"
    )
    assert notice["issue"] is None


def test_target_schema_reports_recorded_and_selected_pack_without_installing():
    from dataclasses import replace

    from astrid.core.generation.entries import target_schema
    from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource

    graph = VibeWorkflow("version-fixture", WorkflowSource("version-fixture"))
    graph.nodes["1"] = VibeNode("1", "FixtureNode")
    graph.metadata["source_provenance"] = _receipt("1.0.0")
    graph.requirements = replace(
        graph.requirements, custom_nodes=["fixture-pack"],
        custom_node_refs=_selected(version="2.0.0", class_set=["FixtureNode"]),
    )
    pending = {"engine": "comfyui", "placement": "runpod", "state": "pending_host_readiness"}
    checked = target_schema({"id": "fixture", "needs": []}, graph, None,
                            pending_target=pending, align="align")
    assert checked.provider is None
    notice, = checked.version_notices
    assert notice["message"] == f"fixture-pack: recorded 1.0.0, installing 2.0.0 ({SECOND})"
    assert notice["issue"]["code"] == "pack_version_differs"
    assert graph.requirements.custom_node_refs[0]["commit"] == SECOND
    result = readiness_envelope(issues=[notice["issue"]], target=pending)
    assert result["status"] == "ready_with_warnings"
    assert result["fixes"] == []


def test_no_recorded_provenance_is_no_version_comparison():
    from dataclasses import replace

    from astrid.core.generation.entries import selected_pack_version_notices
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource

    graph = VibeWorkflow("authored", WorkflowSource("authored"))
    graph.requirements = replace(graph.requirements, custom_node_refs=_selected())
    assert "source_provenance" not in graph.metadata
    assert selected_pack_version_notices(graph, installing=True) == []
    # Empty valid requirements are also an explicit no-selected-packs case.
    graph.requirements = replace(graph.requirements, custom_node_refs=[])
    assert selected_pack_version_notices(graph, installing=True) == []


def test_missing_or_malformed_requirements_refuse_with_typed_issue():
    import pytest

    from astrid.core.generation.entries import target_schema
    from astrid.core.generation.preflight import GenerationReadinessError
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource

    class UnreadableRequirements:
        @property
        def requirements(self):
            raise OSError("PRIVATE_FILE_PATH")

    graph = VibeWorkflow("authored", WorkflowSource("authored"))
    invalid = [object(), UnreadableRequirements()]
    for refs in (None, {"slug": "not-an-array"}, [None], [{}]):
        broken = VibeWorkflow("broken", WorkflowSource("broken"))
        broken.requirements.custom_node_refs = refs
        invalid.append(broken)

    for workflow in invalid:
        with pytest.raises(GenerationReadinessError) as caught:
            target_schema({"id": "fixture", "needs": []}, workflow, None)
        result = caught.value.to_envelope()
        assert result["status"] == "blocked"
        issue, = result["issues"]
        assert issue["code"] == "input_rejected"
        assert issue["details"]["field"] == "requirements.custom_node_refs"
        assert "PRIVATE_FILE_PATH" not in str(result)
