"""D12 retains exact legacy clocks only while their declarations and owners survive."""
from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline.validation import _fps, validate_final_candidate
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.sdk import test_timeline_commit_provider as provider
from tests.sdk.test_p1_doc_patch_provider import _service_call
from tests.sdk.test_timeline_commit_provider_document import document_fixture

runtime_abi = provider.runtime_abi
FPS = {"numerator": 30000, "denominator": 1001}


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def legacy_document():
    document = document_fixture(shared=False)
    parent = document["parent_revision"]
    parent["payload"]["config"]["output"]["fps"] = 30000 / 1001
    parent["payload"]["config"]["theme_overrides"] = {"visual": {"canvas": {"fps": FPS}}}
    parent["content_digest"] = _digest(parent["payload"])
    return document


@pytest.mark.parametrize("edit", ["track", "source"])
def test_untouched_legacy_rates_survive_unrelated_edits_byte_exact(edit):
    baseline = bundle_from_document(legacy_document())
    value = copy.deepcopy(baseline)
    if edit == "track":
        value["parent"]["config"]["tracks"].append({"id": "new", "kind": "visual"})
    else:
        value["shots"]["chapter-shot"]["internal_timeline"]["registry"]["assets"]["picture"]["name"] = "Retained source"
    before = encoded(value)
    validate_final_candidate(value, baseline=baseline)
    assert encoded(value) == before
    assert _fps(30000 / 1001, "fps") != _fps(FPS, "fps")


@pytest.mark.parametrize("damage", [
    "new", "modified", "removed", "equivalent-spelling", "parent-owner",
    "child-owner", "child-revision", "shot-owner", "occurrence-owner", "moved",
])
def test_changed_declarations_or_physical_clock_targets_require_full_parity(damage):
    baseline = bundle_from_document(legacy_document())
    value = copy.deepcopy(baseline)
    config = value["parent"]["config"]
    internal = value["shots"]["chapter-shot"]["internal_timeline"]
    source = value["source_mapping"]["shots"]["chapter-shot"]
    if damage == "new":
        baseline["shots"]["chapter-shot"]["internal_timeline"].pop("output")
    elif damage == "modified":
        config["output"]["fps"] = 24
    elif damage == "removed":
        internal.pop("output")
    elif damage == "equivalent-spelling":
        internal["output"]["fps"] = {"numerator": 60000, "denominator": 2002}
    elif damage == "parent-owner":
        value["base_parent"]["revision_id"] = "replacement-parent"
    elif damage == "child-owner":
        source["internal_timeline_id"] = "replacement-clock"
    elif damage == "child-revision":
        source["internal_timeline_revision_id"] = "replacement-revision"
    elif damage == "shot-owner":
        source["revision_id"] = "replacement-shot"
    elif damage == "occurrence-owner":
        value["placements"][0]["occurrence_id"] = "replacement-occurrence"
    else:
        value["shots"]["replacement"] = value["shots"].pop("chapter-shot")
        value["source_mapping"]["shots"]["replacement"] = value["source_mapping"]["shots"].pop("chapter-shot")
        value["placements"][0]["shot_id"] = "replacement"
    before = encoded(value)
    with pytest.raises(AuthoringBundleError, match="all authored fps"):
        validate_final_candidate(value, baseline=baseline)
    assert encoded(value) == before


@pytest.mark.parametrize("rate", [0, -30, True, "30", float("inf"), float("nan"),
                                  {"numerator": 30000, "denominator": 0},
                                  {"numerator": True, "denominator": 1}])
def test_invalid_scalar_and_rational_rates_are_never_exempted(rate):
    value = bundle_from_document(legacy_document())
    value["parent"]["config"]["output"]["fps"] = rate
    with pytest.raises(AuthoringBundleError):
        validate_final_candidate(value, baseline=copy.deepcopy(value))


@pytest.mark.parametrize("rate", [24, 30, {"numerator": 30000, "denominator": 1001},
                                  {"numerator": 60000, "denominator": 2002}])
def test_new_distinct_rates_are_refused_without_equivalence_policy(rate):
    baseline = bundle_from_document(document_fixture(shared=False))
    value = copy.deepcopy(baseline)
    value["parent"]["config"]["output"]["fps"] = 30000 / 1001
    value["shots"]["chapter-shot"]["internal_timeline"]["output"]["fps"] = rate
    with pytest.raises(AuthoringBundleError, match="all authored fps"):
        validate_final_candidate(value, baseline=baseline)


def test_legacy_parity_exemption_still_runs_structure_reference_schema_and_timing_guards():
    baseline = bundle_from_document(legacy_document())
    for field, value, message in (("track", "missing", "existing track"),
                                  ("at", -1, "must not be negative"),
                                  ("asset", "missing", "unknown asset"),
                                  ("params", {"scale": 0}, "minimum"),
                                  ("app", {"astrid": {"on": "c999"}}, "candidate.timing")):
        candidate = copy.deepcopy(baseline)
        clip = candidate["parent"]["clips"][0]
        clip[field] = value
        if field == "params":
            clip["clipType"] = "am-sprite"
        candidate["parent"]["config"]["clips"] = copy.deepcopy(candidate["parent"]["clips"])
        with pytest.raises(AuthoringBundleError, match=message):
            validate_final_candidate(candidate, baseline=baseline)


def test_fresh_mixed_candidate_requires_parity_and_exact_rational_controls_survive():
    value = bundle_from_document(legacy_document())
    with pytest.raises(AuthoringBundleError, match="all authored fps"):
        validate_final_candidate(value)
    value["parent"]["config"]["output"]["fps"] = copy.deepcopy(FPS)
    validate_final_candidate(value)
    assert _fps(FPS, "fps") == Fraction(30000, 1001)
    assert Fraction(29, 1) / _fps(FPS, "fps") == Fraction(29029, 30000)


def test_coherent_repair_is_allowed_but_a_new_exact_distinct_rational_is_refused():
    baseline = bundle_from_document(legacy_document())
    value = copy.deepcopy(baseline)
    value["parent"]["config"]["output"]["fps"] = copy.deepcopy(FPS)
    validate_final_candidate(value, baseline=baseline)
    value["shots"]["chapter-shot"]["internal_timeline"]["output"]["fps"] = {
        "numerator": 30000000000000001, "denominator": 1001000000000000,
    }
    with pytest.raises(AuthoringBundleError, match="all authored fps"):
        validate_final_candidate(value, baseline=baseline)


@pytest.mark.parametrize("dry_run", [False, True])
def test_production_commit_caller_retains_legacy_fps_with_full_v2_controls(dry_run):
    realm = provider.DetachedRealm(legacy_document())
    original = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    result = _service_call(realm, [provider.track_create()], controls={
        "label": "Create track", "merge": "fields", "reads": [], "dry_run": dry_run,
        "source": {"via": "python"}, "rationale": "Unrelated edit", "group_id": "fps-d12",
        "steps": [{"id": "track", "label": "Create track", "intent": "Add track",
                   "tags": ["test"], "command_ordinals": [0], "patch": [], "reads": []}],
    })
    data = result["data"]
    assert data["status"] == ("preview" if dry_run else "accepted")
    if dry_run:
        assert data["would_status"] == "accepted"
        assert result["receipt"] is None
    else:
        assert result["receipt"]["result"] == data
    payload = data["canonical_document"]["parent_revision"]["payload"]
    assert encoded(payload["config"]["output"]) == encoded(original["parent_revision"]["payload"]["config"]["output"])
    assert encoded(payload["config"]["theme_overrides"]) == encoded(original["parent_revision"]["payload"]["config"]["theme_overrides"])
    assert data["canonical_document"]["internal_timeline_revisions"] == original["internal_timeline_revisions"]
    records = data if dry_run else realm.prepared[-1]["records"]
    assert records["label"] == "Create track"
    assert records["steps"][0]["id"] == "track"
    assert records["steps"][0]["patch"]


def test_production_commit_refuses_modified_mismatch_without_accepting():
    realm = provider.DetachedRealm(legacy_document())
    before = copy.deepcopy(realm.head)
    call = patch(change(["config", "output", "fps"],
                        {"state": "present", "value": 30000 / 1001}, 24))
    with pytest.raises(provider.CommitRejected, match="all authored fps") as raised:
        submit(realm, call, label="Change clock", merge="fields", reads=[],
               steps=[step("clock", 0, call)])
    assert raised.value.failure["http_status"] == 422
    assert raised.value.failure["details"]["status"] == "invalid"
    assert realm.head == before
    assert realm.prepared == []
    assert realm.accepted == []
