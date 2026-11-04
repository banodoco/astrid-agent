"""Finite SDK envelope acceptance; Runtime owns publication policy semantics."""
import copy

import pytest

from astrid.sdk._child_bridge import _validate_child_policy
from astrid.sdk.exceptions import CapabilityValidationError


def policy():
    return {"capabilities": [{"capability_id": "test.raw", "capability_digest": "sha256:" + "a" * 64}],
            "targets": [{"kind": "default"}], "input_object_ids": [], "stages": [{"name": "raw"}],
            "publications": {
                "raw": {"stage": "raw", "output_port": "raw_video", "effect": {"producer": ["template"]}},
                "final": {"stage": "final", "output_port": "final_video", "candidate_stage": "compose",
                          "candidate_output_port": "candidate", "verify_stage": "verify",
                          "verify_output_port": "verification", "effect": {"producer": ["final template"]}}}}


def test_raw_final_envelope_is_preserved_without_recreating_runtime_policy():
    original = policy()
    snapshot = copy.deepcopy(original)
    actual = _validate_child_policy(original)
    assert actual == snapshot
    original["publications"]["raw"]["effect"]["producer"].append("changed")
    assert actual == snapshot


@pytest.mark.parametrize("change", [
    "legacy", "no_stages", "missing_raw", "extra_publication", "private_field", "nonfinite", "nonobject",
])
def test_ambiguous_or_unbounded_publication_envelope_is_rejected(change):
    value = policy()
    if change == "legacy":
        value["final_publication"] = {}
    elif change == "no_stages":
        value.pop("stages")
    elif change == "missing_raw":
        value["publications"].pop("raw")
    elif change == "extra_publication":
        value["publications"]["extra"] = {}
    elif change == "private_field":
        value["publications"]["final"]["verified_publication_source"] = {}
    elif change == "nonfinite":
        value["publications"]["raw"]["effect"]["producer"] = float("nan")
    else:
        value["publications"] = ["raw", "final"]
    with pytest.raises(CapabilityValidationError):
        _validate_child_policy(value)


def test_legacy_envelope_remains_accepted():
    value = policy()
    value.pop("publications")
    value["final_publication"] = {"stage": "final", "verify_stage": "verify", "verify_output_port": "verification", "effect": {}}
    assert _validate_child_policy(value) == value
