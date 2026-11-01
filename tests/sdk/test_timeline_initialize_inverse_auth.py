"""Initialization FPS declarations are removable only by their authenticated inverse."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_initialize import capture_initialize
from tests.editing_conformance.test_doc_patch_v2 import change, patch
from tests.editing_conformance.test_pristine_initialize import empty_document
from tests.sdk import test_timeline_commit_provider as provider
from tests.sdk.test_p1_doc_patch_provider import _service_call

runtime_abi = provider.runtime_abi


def _controls(edits=()):
    return {
        "label": "Timeline edit",
        "merge": "fields",
        "reads": [],
        "source": {"via": "python"},
        "rationale": "Test authenticated initialization undo",
        "group_id": "initialize-inverse-auth",
        "steps": [{
            "id": "edit",
            "label": "Timeline edit",
            "intent": "Edit timeline",
            "tags": [],
            "command_ordinals": [0],
            "patch": copy.deepcopy(list(edits)),
            "reads": [],
        }],
    }


def _initialize(realm):
    bundle = bundle_from_document(realm.documents[realm.head["revision_id"]])
    return capture_initialize(
        bundle,
        name="Main",
        slug="main",
        output={"resolution": "1280x720", "fps": 30, "file": "output.mp4",
                "background": None, "background_scale": None},
    )


def test_exact_authenticated_initialization_inverse_removes_introduced_fps():
    pristine = empty_document()
    realm = provider.DetachedRealm(pristine)
    initialized = _service_call(realm, [_initialize(realm)], controls=_controls())
    assert initialized["data"]["status"] == "accepted"
    fps = initialized["data"]["canonical_document"]["parent_revision"]["payload"]["config"]["output"]["fps"]
    assert fps == 30

    undone = _service_call(
        realm,
        [provider.inverse([initialized["receipt"]["receipt_id"]])],
        controls=_controls(),
    )

    assert undone["data"]["status"] == "accepted"
    assert undone["data"]["canonical_document"] == pristine
    assert undone["data"]["canonical_document"]["parent_revision"]["payload"]["config"].get("output") is None


def test_arbitrary_patch_cannot_remove_introduced_fps():
    realm = provider.DetachedRealm(empty_document())
    initialized = _service_call(realm, [_initialize(realm)], controls=_controls())
    output = initialized["data"]["canonical_document"]["parent_revision"]["payload"]["config"]["output"]
    removal = change(["config", "output", "fps"],
                     {"state": "present", "value": output["fps"]}, op="remove")
    controls = _controls([removal])

    with pytest.raises(provider.CommitRejected, match="all authored fps"):
        _service_call(realm, [patch(removal)], controls=controls)

    assert realm.head["publication_id"] == initialized["receipt"]["receipt_id"]
    assert realm.accepted[-1]["data"]["canonical_document"] == initialized["data"]["canonical_document"]
