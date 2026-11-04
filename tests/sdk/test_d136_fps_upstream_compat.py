"""D136 preserves the upstream two-integer FPS spelling through the provider."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline import _commit_timing_validation as timing_validation
from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._timing_evaluators import _fps as production_fps
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline.native_initialize import capture_initialize
from astrid.sdk.timeline.validation import (
    FinalCandidateError,
    _authenticated_initialization_fps_removals,
    _validate_fps,
)
from tests.editing_conformance.test_doc_patch_v2 import change, patch
from tests.editing_conformance.test_pristine_initialize import empty_document
from tests.sdk import test_timeline_commit_provider as provider
from tests.sdk.test_p1_doc_patch_provider import _service_call
from tests.sdk.test_timeline_commit_provider_document import document_fixture

runtime_abi = provider.runtime_abi


def _document_with_upstream_fps_list() -> dict:
    document = document_fixture(shared=False)
    parent = document["parent_revision"]
    parent["payload"]["config"]["theme_overrides"] = {
        "visual": {"canvas": {"fps": [30000, 1001]}}
    }
    parent["content_digest"] = _digest(parent["payload"])
    return document


def _controls(*, dry_run: bool) -> dict:
    return {
        "label": "Unrelated track edit",
        "merge": "fields",
        "reads": [],
        "dry_run": dry_run,
        "source": {"via": "python"},
        "rationale": "Preserve upstream FPS spelling",
        "group_id": "d136-fps",
        "steps": [{
            "id": "track",
            "label": "Unrelated track edit",
            "intent": "Add an unrelated track",
            "tags": ["d136"],
            "command_ordinals": [0],
            "patch": [],
            "reads": [],
        }],
    }


def _document_with_canvas(canvas) -> dict:
    document = document_fixture(shared=False)
    parent = document["parent_revision"]
    parent["payload"]["config"]["theme_overrides"] = {"visual": {"canvas": canvas}}
    parent["content_digest"] = _digest(parent["payload"])
    return document


@pytest.mark.parametrize("dry_run", [False, True])
def test_production_caller_accepts_and_retains_upstream_fps_list(dry_run):
    realm = provider.DetachedRealm(_document_with_upstream_fps_list())
    original = copy.deepcopy(realm.documents[realm.head["revision_id"]])

    result = _service_call(
        realm,
        [provider.track_create("d136-track")],
        controls=_controls(dry_run=dry_run),
    )

    assert result["data"]["status"] == ("preview" if dry_run else "accepted")
    if dry_run:
        assert result["data"]["would_status"] == "accepted"
        assert result["receipt"] is None
    else:
        assert result["receipt"]["result"] == result["data"]
    payload = result["data"]["canonical_document"]["parent_revision"]["payload"]
    assert payload["config"]["theme_overrides"] == original["parent_revision"]["payload"]["config"]["theme_overrides"]


@pytest.mark.parametrize("fps", [[30000], [30000, 0], {"numerator": 0, "denominator": 1001}])
def test_production_caller_keeps_invalid_typed_fps_refused(fps):
    document = document_fixture(shared=False)
    parent = document["parent_revision"]
    parent["payload"]["config"]["output"]["fps"] = fps
    parent["content_digest"] = _digest(parent["payload"])
    realm = provider.DetachedRealm(document)
    before = copy.deepcopy(realm.head)

    with pytest.raises(provider.CommitRejected) as raised:
        _service_call(
            realm,
            [provider.track_create("d136-invalid-fps")],
            controls=_controls(dry_run=False),
        )

    assert raised.value.failure["http_status"] == 422
    assert raised.value.failure["details"]["status"] == "invalid"
    assert realm.head == before
    assert realm.prepared == []
    assert realm.accepted == []


@pytest.mark.parametrize(
    ("label", "canvas", "effective_fps"),
    [
        ("canvas-null", None, 30),
        ("canvas-bool", True, 30),
        ("canvas-string", "canvas", 30),
        ("canvas-list", [], 30),
        ("fps-null", {"fps": None}, 30),
        ("fps-true", {"fps": True}, 1),
        ("fps-false", {"fps": False}, 30),
        ("fps-string", {"fps": "30"}, 30),
        ("fps-mapping", {"fps": {"arbitrary": 1}}, 30),
        ("fps-zero", {"fps": 0}, 30),
        ("fps-negative", {"fps": -1}, 30),
        ("fps-empty", {"fps": []}, 30),
        ("fps-one", {"fps": [30000]}, 30),
        ("fps-three", {"fps": [30000, 1001, 1]}, 30),
        ("fps-noninteger", {"fps": [30000.0, 1001]}, 30),
        ("fps-zero-pair", {"fps": [30000, 0]}, 30),
        ("fps-negative-pair", {"fps": [-30000, 1001]}, 30),
        ("fps-rational-list", {"fps": [30000, 1001]}, 30),
        ("fps-rational-map", {"fps": {"numerator": 30000, "denominator": 1001}}, 30),
        ("fps-positive-scalar", {"fps": 24}, 24),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_open_canvas_forms_follow_upstream_fallback_through_production_provider(
    label, canvas, effective_fps, dry_run, monkeypatch,
):
    del label
    observed = []
    original_compile = timing_validation.compile_timing

    def capture(inputs):
        observed.append(production_fps(inputs.bundle))
        return original_compile(inputs)

    monkeypatch.setattr(timing_validation, "compile_timing", capture)
    document = _document_with_canvas(canvas)
    original = copy.deepcopy(document["parent_revision"]["payload"]["config"]["theme_overrides"])
    realm = provider.DetachedRealm(document)

    result = _service_call(
        realm,
        [provider.track_create("d136-open-canvas")],
        controls=_controls(dry_run=dry_run),
    )

    data = result["data"]
    assert data["status"] == ("preview" if dry_run else "accepted")
    if dry_run:
        assert data["would_status"] == "accepted"
        assert result["receipt"] is None
    else:
        assert result["receipt"]["result"] == data
    assert observed[-1] == effective_fps
    payload = data["canonical_document"]["parent_revision"]["payload"]
    assert payload["config"]["theme_overrides"] == original


@pytest.mark.parametrize("dry_run", [False, True])
def test_capture_source_initialization_adds_typed_fps_without_normalization(dry_run, monkeypatch):
    # Exact first commit in test_capture_source_runtime.open_source_runtime used by the
    # unchanged f53b projection fixture; no media/Store setup is needed here.
    document = empty_document()
    before = copy.deepcopy(document)
    realm = provider.DetachedRealm(document)
    head = copy.deepcopy(realm.head)
    output = {"resolution": "1280x720", "fps": 30, "file": "output.mp4",
              "background": None, "background_scale": None}
    command = capture_initialize(
        bundle_from_document(document), name="Main", slug="main", output=output,
    )
    observed = []
    original_compile = timing_validation.compile_timing

    def capture(inputs):
        observed.append(production_fps(inputs.bundle))
        return original_compile(inputs)

    monkeypatch.setattr(timing_validation, "compile_timing", capture)
    result = _service_call(realm, [command], controls=_controls(dry_run=dry_run))

    data = result["data"]
    assert data["status"] == ("preview" if dry_run else "accepted")
    if dry_run:
        assert data["would_status"] == "accepted"
        assert result["receipt"] is None
        assert realm.head == head
        assert realm.accepted == []
        assert realm.prepared == []
    else:
        assert result["receipt"]["result"] == data
    payload = data["canonical_document"]["parent_revision"]["payload"]
    assert payload["config"]["output"] == output
    assert payload["name"] == "Main"
    assert payload["slug"] == "main"
    assert "theme_overrides" not in payload["config"]
    assert observed and all(fps == 30 for fps in observed)
    assert document == before
    assert realm.documents[head["revision_id"]] == before


@pytest.mark.parametrize("damage", ["parent-remove", "child-remove", "move-to-open", "change-rate"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_existing_typed_fps_cannot_be_removed_moved_or_changed(damage, dry_run):
    document = document_fixture(shared=False)
    rate = document["parent_revision"]["payload"]["config"]["output"]["fps"]
    ref = "internal:chapter-1" if damage == "child-remove" else "parent"
    path = ["output", "fps"] if damage == "child-remove" else ["config", "output", "fps"]
    edits = [change(path, {"state": "present", "value": rate}, op="remove", ref=ref)]
    if damage == "move-to-open":
        edits.append(change(["config", "theme_overrides"], {"state": "absent"},
                            {"visual": {"canvas": {"fps": rate}}}))
    elif damage == "change-rate":
        edits = [change(path, {"state": "present", "value": rate}, 24)]
    realm = provider.DetachedRealm(document)
    head = copy.deepcopy(realm.head)
    controls = _controls(dry_run=dry_run)
    controls["steps"][0]["patch"] = copy.deepcopy(edits)

    with pytest.raises(provider.CommitRejected, match="all authored fps") as raised:
        _service_call(realm, [patch(*edits)], controls=controls)

    assert raised.value.failure["http_status"] == 422
    assert raised.value.failure["details"]["status"] == "invalid"
    assert realm.head == head
    assert realm.prepared == []
    assert realm.accepted == []
    assert realm.documents[head["revision_id"]] == document


@pytest.mark.parametrize("dry_run", [False, True])
def test_initialization_still_requires_the_exact_base_pair(dry_run):
    document = empty_document()
    realm = provider.DetachedRealm(document)
    head = copy.deepcopy(realm.head)
    command = capture_initialize(
        bundle_from_document(document), name="Main", slug="main",
        output={"resolution": "1280x720", "fps": 30, "file": "output.mp4",
                "background": None, "background_scale": None},
    )

    with pytest.raises(provider.CommitRejected):
        _service_call(realm, [command], controls=_controls(dry_run=dry_run),
                      base={**head, "publication_id": "unrelated-publication"})

    assert realm.head == head
    assert realm.prepared == []
    assert realm.accepted == []
    assert realm.documents[head["revision_id"]] == document


def test_fps_removal_requires_selected_authenticated_initialization_inverse():
    before = {"parent": {"config": {"tracks": []}}, "shots": {}}
    after = {"parent": {"config": {"tracks": [], "output": {"fps": 30}}}, "shots": {}}
    baseline = {"parent": {"config": {"tracks": [], "output": {"fps": 24}}}, "shots": {}}
    candidate = {"parent": {"config": {"tracks": []}}, "shots": {}}
    source = {"ordinal": 0, "command": {"op": "timeline.init"}, "writes": [{
        "ref": "parent", "path": ["config", "output"],
        "before": {"state": "absent"}, "after": {"state": "present", "value": {"fps": 30}},
    }]}
    entry = {"publication_id": "initialize", "records": {"commands": [source]}}
    accepted_init = AcceptedRecords([entry], [source], lambda _: (before, after))
    inverse_record = {"command": {"op": "timeline.inverse"}, "writes": [{
        "ref": "parent", "path": ["config", "output"],
        "before": {"state": "present", "value": {"fps": 24}},
        "after": {"state": "absent"},
    }]}

    assert _authenticated_initialization_fps_removals(
        candidate, baseline, [inverse_record], accepted_init,
    ) == {"parent.config.output.fps"}

    source["command"] = {"op": "doc.patch"}
    assert _authenticated_initialization_fps_removals(
        candidate, baseline, [inverse_record], accepted_init,
    ) == set()


def test_inverse_cannot_remove_fps_from_an_existing_shot():
    before = {"parent": {"config": {}}, "shots": {
        "existing": {"internal_timeline": {"output": {}}},
    }}
    after = {"parent": {"config": {}}, "shots": {
        "existing": {"internal_timeline": {"output": {"fps": 30}}},
    }}
    baseline = copy.deepcopy(after)
    candidate = copy.deepcopy(before)
    source = {"ordinal": 0, "command": {"op": "doc.patch"}, "created": [], "writes": [{
        "ref": "shot:existing", "path": ["internal_timeline", "output", "fps"],
        "before": {"state": "absent"}, "after": {"state": "present", "value": 30},
    }]}
    entry = {"publication_id": "add-shot-fps", "records": {"commands": [source]}}
    accepted_patch = AcceptedRecords([entry], [source], lambda _: (before, after))
    inverse_record = {"command": {"op": "timeline.inverse"}, "writes": [{
        "ref": "shot:existing", "path": ["internal_timeline", "output", "fps"],
        "before": {"state": "present", "value": 30}, "after": {"state": "absent"},
    }]}

    with pytest.raises(FinalCandidateError, match="all authored fps"):
        _validate_fps(candidate, baseline, [inverse_record], accepted_patch)
