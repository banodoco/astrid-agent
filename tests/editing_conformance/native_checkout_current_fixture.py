"""Current-form topology for the four native-checkout companions.

The source media, transcript pin, and word choice come from the registered
current fixture.  The parent audio clips are then admitted through the normal
document emitter so the checkout tests exercise the same public topology as
their retained counterparts.
"""
from __future__ import annotations

import copy

from tests.editing_conformance import current_word_baseline_fixture as current_inputs
from tests.editing_conformance.test_native_word_anchor import bundle as current_bundle
from tests.editing_conformance.test_native_word_anchor import pick as current_pick

retained = current_inputs.retained


def _current_commit(case, commands, key):
    result = case.submit(case.request(commands), key)
    assert result["data"]["status"] in {"accepted", "rebased"}
    return result


def prepare(case, *, gate3: bool = False):
    """Add the checkout target topology through the authenticated current seam."""
    binding = current_pick(case, 1, text=current_inputs.DECLARATION)
    bound = _current_commit(case, [binding], "native-checkout-current-binding")
    assert binding["args"]["choice"]["document"] == case.pin
    assert binding["args"]["choice"]["source"] == "occurrence:occ/clip:voice"
    assert bound["data"]["canonical_document"]["dependency_manifest"]["documents"] == [case.pin]

    from astrid.sdk.timeline.emitter import emit_document_commands

    media_id = case.source["content"]["media_id"]
    authored = current_bundle(case)
    desired = copy.deepcopy(authored)
    desired["parent"]["registry"]["assets"]["music-bed"] = {
        "media_id": media_id,
        "type": "audio",
    }
    desired["parent"]["config"]["tracks"].extend([
        {"id": "music", "kind": "audio"},
        {"id": "music-other", "kind": "audio"},
    ])
    for clip_id, track_id in (("music-bed", "music"), ("music-other", "music-other")):
        desired["parent"]["clips"].append({
            "id": clip_id,
            "clipType": "audio",
            "asset": "music-bed",
            "track": track_id,
            "at": 0,
            "hold": 5.5,
            "extension": {"opaque": [None, {"keep": True}]},
        })
    desired["parent"]["config"]["clips"] = copy.deepcopy(desired["parent"]["clips"])
    topology = _current_commit(
        case,
        emit_document_commands(authored, desired),
        "native-checkout-current-topology",
    )
    parent = topology["data"]["canonical_document"]["parent_revision"]["payload"]
    assert parent["config"]["clips"] == parent["clips"]
    assert [track["id"] for track in parent["config"]["tracks"][-2:]] == [
        "music", "music-other",
    ]
    assert {
        clip["id"]: clip["track"] for clip in parent["clips"][-2:]
    } == {"music-bed": "music", "music-other": "music-other"}
    assert parent["registry"]["assets"]["music-bed"] == {
        "media_id": media_id,
        "type": "audio",
    }
    assert [clip["id"] for clip in parent["clips"][-2:]] == ["music-bed", "music-other"]

    if gate3:
        authored = current_bundle(case)
        desired = copy.deepcopy(authored)
        desired["parent"]["occurrences"] = []
        desired["parent"]["clips"].append({
            "id": "gate3-title",
            "at": 0,
            "track": "picture",
            "clipType": "text-card",
            "hold": 1,
            "params": {"content": "Gate3"},
            "elementRef": {"packId": "rendering", "kind": "effects", "id": "text-card"},
        })
        desired["parent"]["config"]["clips"] = copy.deepcopy(desired["parent"]["clips"])
        topology = _current_commit(
            case,
            emit_document_commands(authored, desired),
            "native-checkout-current-gate3",
        )
        parent = topology["data"]["canonical_document"]["parent_revision"]["payload"]
        assert parent["occurrences"] == []
        assert parent["clips"][-1]["elementRef"]["id"] == "text-card"

    return {
        "case": case,
        "media_id": media_id,
        "source_document": copy.deepcopy(case.seed["data"]["canonical_document"]),
        "parent": copy.deepcopy(parent),
        "binding": copy.deepcopy(binding),
    }
