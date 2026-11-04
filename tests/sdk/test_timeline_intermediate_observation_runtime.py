"""Replay SDK ripple observations at Runtime's exact command boundaries."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._doc_patch import value_at
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline.emitter import emit_document_commands
from tests.helpers.runtime import use_runtime
from tests.sdk.test_timeline_commit_provider_document import document_fixture
from tests.sdk.test_timeline_flat_reflow_commit import _flat_audio_picture_fixture


@pytest.mark.parametrize("operation,music_at,music_policy", [
    ("insert_time", None, "keep_end"),
    ("ripple_delete", None, "keep_end"),
    ("close_gap", None, "keep_end"),
    ("ripple_delete", 4, "keep_end"),
    ("ripple_delete", 5, "keep_end"),
    ("ripple_delete", 4, "plays_on"),
    ("ripple_delete", 5, "plays_on"),
])
def test_sdk_ripple_has_exact_authenticated_intermediate_inputs(
    operation, music_at, music_policy,
):
    use_runtime()
    from runtime_protocol.timeline_commit_auth import command_output, images

    document = document_fixture(shared=False)
    revision = document["parent_revision"]["revision_id"]
    realm = SimpleNamespace(documents={revision: document}, head={"revision_id": revision})
    document = _flat_audio_picture_fixture(realm, short_gap=operation == "close_gap")
    parent = document["parent_revision"]["payload"]
    if music_at is not None:
        bed = {"id": "music-bed", "clipType": "media", "track": "music", "asset": "voice",
               "at": music_at, "from": 0, "to": 10}
        parent["clips"].append(bed)
        parent["config"]["clips"] = copy.deepcopy(parent["clips"])
        parent["config"]["tracks"].append({"id": "music", "kind": "audio"})
        document["parent_revision"]["content_digest"] = _digest(parent)
    original = copy.deepcopy(document)
    bundle = bundle_from_document(document)
    bundle["base_parent"]["publication_id"] = "bootstrap"
    checkout = Checkout(copy.deepcopy(bundle))
    if operation == "insert_time":
        checkout.insert_time(4, 2)
    elif operation == "ripple_delete":
        checkout.ripple_delete(4, 6, music=music_policy)
    else:
        word = next(word for word in checkout.words() if word.text == "after")
        checkout.close_gap(before=word.id)
    commands = emit_document_commands(bundle, checkout.bundle)
    staged = execute(bundle, commands).bundle
    commands.append({
        "schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
        "args": {"changes": [{
            "op": "set", "ref": "parent", "path": ["name"],
            "before": value_at(staged["parent"], "parent", ["name"]), "value": "After ripple",
        }]},
    })
    head = {"revision_id": revision, "publication_id": "bootstrap"}
    request = {
        "api_version": 1, "project_id": bundle["project_id"], "timeline_id": bundle["timeline_id"],
        "base_revision_id": revision, "base_publication_id": "bootstrap", "commands": commands,
        "label": "Ripple then rename", "steps": [{
            "id": "ripple", "label": "Ripple then rename", "intent": "", "tags": [],
            "command_ordinals": list(range(len(commands))),
            "patch": [copy.deepcopy(change) for command in commands
                      for change in command["args"]["changes"]],
        }],
    }
    plan = execute(bundle, commands, commit_request=request, evaluated_head=head)

    for index, record in enumerate(plan.records[:-1]):
        execution = plan._sequential_execution[index]
        projected = command_output(images(execution["input"]), record, plan.records[index + 1])
        assert projected == images(execution["output"])
        assert projected == images(plan._sequential_execution[index + 1]["input"])
    assert document == original


def test_sdk_nested_patch_observes_its_preceding_container_write():
    use_runtime()
    from runtime_protocol.timeline_commit_auth import command_output, images

    document = document_fixture(shared=False)
    bundle = bundle_from_document(document)
    bundle["base_parent"]["publication_id"] = "bootstrap"
    first = {"schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
             "args": {"changes": [
                 {"op": "set", "ref": "parent", "path": ["extension"],
                  "before": {"state": "absent"}, "value": {"count": 1}},
                 {"op": "set", "ref": "parent", "path": ["extension", "count"],
                  "before": {"state": "present", "value": 1}, "value": 2},
             ]}}
    following = {"schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
                 "args": {"changes": [{
                     "op": "set", "ref": "parent", "path": ["extension", "count"],
                     "before": {"state": "present", "value": 2}, "value": 3,
                 }]}}
    commands = [first, following]
    head = {"revision_id": bundle["base_parent"]["revision_id"], "publication_id": "bootstrap"}
    request = {
        "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
        "commands": commands, "steps": [{
            "id": "nested", "label": "Nested patch", "intent": "", "tags": [],
            "command_ordinals": [0, 1],
            "patch": [change for command in commands for change in command["args"]["changes"]],
        }],
    }
    plan = execute(bundle, commands, commit_request=request, evaluated_head=head)

    projected = command_output(images(plan._sequential_execution[0]["input"]),
                               plan.records[0], plan.records[1])
    assert projected == images(plan._sequential_execution[1]["input"])
    assert projected["parent"]["payload"]["extension"] == {"count": 2}
