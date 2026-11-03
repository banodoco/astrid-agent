"""Pure conformance vectors for the bundle-to-doc.patch emitter."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline.emitter import DocumentPatchError, emit_document_commands

FIXTURE = Path(__file__).parents[1] / "fixtures" / "timeline_editing" / "tiny.json"


def bundle():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def changes(before, after):
    commands = emit_document_commands(before, after)
    assert len(commands) == 1
    return commands[0]["args"]["changes"]


def test_scalar_is_id_addressed_and_keeps_previous_value():
    before = bundle()
    after = copy.deepcopy(before)
    after["parent"]["config"]["theme_overrides"]["visual"]["canvas"]["fps"] = 24

    assert changes(before, after) == [{
        "op": "set", "ref": "parent",
        "path": ["config", "theme_overrides", "visual", "canvas", "fps"],
        "before": {"state": "present", "value": 30}, "value": 24,
    }]


def test_absent_null_empty_and_opaque_values_are_not_normalized():
    before = bundle()
    before["parent"]["meta"] = {"opaque.key": {"empty": [], "value": None}}
    after = copy.deepcopy(before)
    after["parent"]["meta"]["opaque.key"]["empty"] = {}
    after["parent"]["meta"]["opaque.key"]["added"] = None
    found = changes(before, after)

    assert found == [
        {"op": "set", "ref": "parent", "path": ["meta", "opaque.key", "added"],
         "before": {"state": "absent"}, "value": None},
        {"op": "set", "ref": "parent", "path": ["meta", "opaque.key", "empty"],
         "before": {"state": "present", "value": []}, "value": {}},
    ]


def test_identity_list_uses_ordered_insert_and_field_changes():
    before = bundle()
    before["parent"]["config"]["tracks"] = [{"id": "a", "kind": "visual"}, {"id": "b", "kind": "audio"}]
    after = copy.deepcopy(before)
    after["parent"]["config"]["tracks"] = [
        {"id": "b", "kind": "audio", "label": "Sound"},
        {"id": "a", "kind": "visual"},
        {"id": "c", "kind": "visual"},
    ]
    found = changes(before, after)

    assert found[0]["op"] == "list.insert" and found[0]["id"] == "c"
    assert found[1]["op"] == "list.move" and found[1]["id"] == "b"
    assert found[2]["path"] == ["config", "tracks", "id:b", "label"]
    assert found[2]["before"] == {"state": "absent"} and found[2]["value"] == "Sound"


def test_idless_lists_are_one_guarded_value():
    before = bundle()
    before["parent"]["meta"] = {"tags": ["rough", "review"]}
    after = copy.deepcopy(before)
    after["parent"]["meta"]["tags"] = ["approved"]

    assert changes(before, after) == [{
        "op": "set", "ref": "parent", "path": ["meta", "tags"],
        "before": {"state": "present", "value": ["rough", "review"]},
        "value": ["approved"],
    }]


@pytest.mark.parametrize("edit", [
    lambda value: value["placements"].pop(),
    lambda value: value["shots"].pop("A"),
    lambda value: value.setdefault("source_mapping", {}).update({"changed": True}),
])
def test_closure_and_provenance_changes_refuse_instead_of_root_overwrite(edit):
    before = bundle()
    after = copy.deepcopy(before)
    edit(after)
    with pytest.raises(DocumentPatchError, match="semantic command|provenance"):
        emit_document_commands(before, after)


def test_no_change_emits_no_request():
    value = bundle()
    assert emit_document_commands(value, copy.deepcopy(value)) == []
