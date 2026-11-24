"""LE-10: genuine graph-verified doctor labels have a linked repair."""
import json

from astrid.core.generation.managed_preparation import (
    native_failure_inputs, native_unknown_input_readiness,
)


def _removed_input():
    stdout = json.dumps({"status": "error", "layer": "VibeWorkflow validation",
                         "validation_errors": [{"node_id": "1", "class_type": "FixtureNode",
                                                "errors": [{"code": "unknown_input", "input": "old"}]}]})
    labels = native_failure_inputs(stdout, "workflow_validation_failed",
                                   (("1", "FixtureNode", "old"),))
    return native_unknown_input_readiness(labels)


def _pack_warning(recorded_version, selected_version=None):
    from astrid.core.generation.entries import pack_version_notices
    from astrid.core.generation.preflight import readiness_envelope
    from vibecomfy.porting.provenance import extract_provenance

    report = extract_provenance({"nodes": [
        {"id": 1, "type": "FixtureNode",
         "properties": {"cnr_id": "fixture-pack", "ver": recorded_version}},
    ]}).to_json()
    ref = {"slug": "fixture-pack", "source": "git", "url": "https://example.test/fixture-pack",
           "commit": "a" * 40}
    if selected_version is not None:
        ref["version"] = selected_version
    notice, = pack_version_notices(report, [ref], installing=True)
    result = readiness_envelope(issues=[notice["issue"]])
    assert [issue["code"] for issue in result["issues"]] == ["pack_version_differs"]
    assert result["status"] == "ready_with_warnings"
    assert result["fixes"] == []
    assert result["issues"][0]["details"]["recorded_versions"] == [recorded_version]
    assert result["issues"][0]["details"]["selected_commit"] == "a" * 40
    return result


def _different_version_label():
    return _pack_warning("1.0.0", "2.0.0")


def _different_exact_commit():
    return _pack_warning("b" * 40)


CASES = [
    ("live-doctor-removed-input", "unknown_input", _removed_input),
    ("different-version-label-warning", "pack_version_differs", _different_version_label),
    ("different-exact-commit-warning", "pack_version_differs", _different_exact_commit),
]
