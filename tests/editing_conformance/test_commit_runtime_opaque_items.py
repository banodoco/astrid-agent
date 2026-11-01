"""Unknown namespaced values survive real occurrence materialization."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._command_state import present
from tests.editing_conformance.test_commit_runtime_identity import occurrence_image
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration
NAMESPACE = "org.example.notes/v1"


def opaque():
    return {NAMESPACE: {
        "foreign_item_id": "item-1", "foreign_item_ids": ["item-2", None, "item-1"],
        "item_id": "item-1", "item_ids": ["item-1", "item-2"],
        "literal": "item-1", "item-1": None,
        "nested": [{"selected_item_id": "item-2", "parent_item_id": "item-1"}, None],
    }}


def occurrence_patch(before, rows):
    """Capture authored occurrence fields without changing managed edges."""
    current = {row["occurrence_id"]: row for row in before["parent_revision"]["payload"]["occurrences"]}
    order = [row["occurrence_id"] for row in before["parent_revision"]["payload"]["occurrences"]]
    target_order = [row["occurrence_id"] for row in rows]
    changes = []
    for index, identity in enumerate(target_order):
        if order[index] == identity:
            continue
        row = copy.deepcopy(current[identity])
        changes.append({
            "op": "list.move", "ref": "parent", "path": ["occurrences"],
            "id": identity, "before": present(row), "order_before": list(order),
            "after_id": target_order[index - 1] if index else None,
        })
        old_index = order.index(identity)
        moved = order.pop(old_index)
        insert_at = order.index(target_order[index - 1]) + 1 if index else 0
        order.insert(insert_at, moved)
    for row in rows:
        identity = row["occurrence_id"]
        if current[identity] == row:
            continue
        changes.append({
            "op": "set", "ref": "parent",
            "path": ["occurrences", "occurrence_id:" + identity],
            "before": present(current[identity]), "value": copy.deepcopy(row),
        })
    return {"schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
            "args": {"changes": changes}}


def body_with_ordered_patch(edit, commands):
    """Claim the exact ordered effects for a doc.patch-only setup commit."""
    body = edit.body(commands)
    body["steps"][0]["patch"] = copy.deepcopy([
        change
        for command in commands
        if command["op"] == "doc.patch"
        for change in command["args"]["changes"]
    ])
    return body


@pytest.fixture(params=["item_id", "id", "both"])
def opaque_film(request, tmp_path, monkeypatch):
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / "tests"))
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from test_timeline_commit_integration import Editing
    from timeline_commit_film_fixture import build_film_fixture

    from astrid.sdk.timeline.capture import capture_move
    from astrid.sdk.timeline.commit import (
        _digest,
        _manifest,
        bundle_from_document,
    )
    from astrid.sdk.timeline.emitter import emit_document_commands
    from astrid.sdk.timeline.native_occurrence_delete import capture_occurrence_delete
    from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared

    fixture = build_film_fixture(tmp_path)
    service = RuntimeService(fixture.duplicate_root)
    edit = Editing(service, SimpleNamespace(fixture=fixture),
                   SimpleNamespace(move=capture_move, bundle=bundle_from_document))
    try:
        service.register_timeline_commit_provider(load_timeline_commit_provider("astrid"))
        original = edit.document()

        # Open occurrence-2 only through the real semantic route.  The source
        # occurrence is then a genuine shared physical closure for the opaque
        # payload patch below; an independently rewritten source would bypass
        # the materialization case this fixture is meant to cover.
        deleted = edit.commit(edit.body([capture_occurrence_delete(
            bundle_from_document(original), "occurrence:occurrence-2",
        )]), "opaque-delete-source")
        staged = deleted["data"]["canonical_document"]
        target = copy.deepcopy(staged)
        parent = target["parent_revision"]["payload"]
        _, shot, internal = occurrence_image(target, "occurrence-1")
        internal["payload"]["clips"][0]["hold"] = 0.25
        media = shot["payload"]["provenance"]["source_media"]
        shot["payload"]["items"] = [{
            "item_id": item_id, "media_id": media, "source_frame": None,
            "metadata": {"source_item_id": "item-1", "source_item": "item-2", "data": opaque()},
            "data": opaque(), "app": opaque(), "opaque": opaque(),
        } for item_id in ("item-1", "item-2")]
        for item in shot["payload"]["items"]:
            if request.param in ("id", "both"):
                item["id"] = item["item_id"]
            if request.param == "id":
                del item["item_id"]
        for row in (parent, parent["config"], parent["config"]["tracks"][0],
                    parent["clips"][0], shot["payload"], internal["payload"],
                    internal["payload"]["tracks"][0], internal["payload"]["clips"][0]):
            row.update(data=opaque(), app=opaque(), opaque=opaque())
        parent["occurrences"][0].update(data=opaque(), app=opaque(), opaque=opaque())
        target["parent_revision"]["content_digest"] = _digest(parent)
        for image in target["shot_revisions"]:
            image["content_digest"] = _digest(image["payload"])
        for image in target["internal_timeline_revisions"]:
            image["content_digest"] = _digest(image["payload"])
        target["dependency_manifest"] = _manifest(
            target["shot_revisions"], target["internal_timeline_revisions"],
            target["dependency_manifest"]["media"], target["dependency_manifest"]["documents"],
        )
        staged_bundle = bundle_from_document(staged)
        target_bundle = bundle_from_document(target)
        authored_target = copy.deepcopy(staged_bundle)
        authored_target["parent"] = target_bundle["parent"]
        for local, shot in authored_target["shots"].items():
            shot["payload"] = target_bundle["shots"][local]["payload"]
            shot["internal_timeline"] = target_bundle["shots"][local]["internal_timeline"]
        setup = emit_document_commands(staged_bundle, authored_target)
        source_row = copy.deepcopy(staged["parent_revision"]["payload"]["occurrences"][0])
        source_row.update(data=opaque(), app=opaque(), opaque=opaque())
        setup.append(occurrence_patch(staged, [source_row, *staged["parent_revision"]["payload"]["occurrences"][1:]]))
        source = edit.commit(body_with_ordered_patch(edit, setup), "opaque-source")

        source_document = source["data"]["canonical_document"]
        shared = capture_occurrence_place_shared(
            bundle_from_document(source_document), "occurrence:occurrence-1", "occurrence-2", on=1,
        )
        shared_result = edit.commit(edit.body([shared]), "opaque-shared-source")
        shared_document = shared_result["data"]["canonical_document"]

        # Restore the premigration occurrence rows and order while retaining
        # the authenticated source revision returned by place-shared.
        current = {row["occurrence_id"]: row for row in
                   shared_document["parent_revision"]["payload"]["occurrences"]}
        original_rows = {
            row["occurrence_id"]: row
            for row in original["parent_revision"]["payload"]["occurrences"]
        }
        rows = []
        for occurrence_id in (
            row["occurrence_id"] for row in original["parent_revision"]["payload"]["occurrences"]
        ):
            row = copy.deepcopy(original_rows[occurrence_id])
            if occurrence_id in ("occurrence-1", "occurrence-2"):
                source_row = current[occurrence_id]
                row["shot_id"] = source_row["shot_id"]
                row["shot_revision_id"] = source_row["shot_revision_id"]
                row.update(data=opaque(), app=opaque(), opaque=opaque())
            rows.append(row)
        baseline = edit.commit(
            body_with_ordered_patch(edit, [occurrence_patch(shared_document, rows)]),
            "opaque-baseline",
        )
        baseline_document = baseline["data"]["canonical_document"]
        first = occurrence_image(baseline_document, "occurrence-1")
        second = occurrence_image(baseline_document, "occurrence-2")
        assert first[1]["revision_id"] == second[1]["revision_id"]
        assert first[1]["internal_timeline_revision_id"] == second[1]["internal_timeline_revision_id"]
        yield edit, load_timeline_commit_provider
    finally:
        edit.service.close()


def assert_opaque(row):
    for field in ("data", "app", "opaque"):
        # The moved clip's app.astrid timing fields are declared writes.
        assert {key: value for key, value in row[field].items() if key != "astrid"} == opaque()


def assert_readback(edit, document):
    assert edit.document(document["parent_revision"]["revision_id"]) == document
    for shot in document["shot_revisions"]:
        stored = edit.service.get_project_shot_revision(edit.project, shot["shot_id"], shot["revision_id"])
        assert stored["payload"] == shot["payload"]
        assert stored["content_digest"] == shot["content_digest"]
    for internal in document["internal_timeline_revisions"]:
        stored = edit.service.get_project_timeline_revision(
            edit.project, edit.timeline, internal["revision_id"])
        assert stored["payload"] == internal["payload"]
        assert stored["content_digest"] == internal["content_digest"]


def test_occurrence_move_preserves_unknown_item_ids_retry_restart_and_inverse(
    opaque_film, tmp_path, monkeypatch,
):
    from runtime_protocol.service import RuntimeService

    edit, provider = opaque_film
    original = edit.document()
    original_copy = copy.deepcopy(original)
    command = edit.agent.move(edit.agent.bundle(original), "occurrence:occurrence-1/clip:clip-1", 0.5)
    body = edit.body([command])
    flight = tmp_path / "flight.json"
    flight.write_text(json.dumps({"key": "opaque-lost-ack", "body": body}))
    accepted = edit.commit(body, "opaque-lost-ack")
    assert accepted["data"]["status"] == "accepted"
    document = accepted["data"]["canonical_document"]
    history = edit.history()
    writes = history[-1]["records"]["commands"][0]["writes"]
    _, _, internal = occurrence_image(document, "occurrence-1")
    assert internal["payload"]["clips"][0]["at"] == 0.5
    assert not any("data" in write["path"] or "opaque" in write["path"] for write in writes)
    assert edit.document() == document
    assert original == original_copy
    assert edit.document(original["parent_revision"]["revision_id"]) == original
    for sibling in ("occurrence-2", "occurrence-3"):
        assert occurrence_image(document, sibling) == occurrence_image(original, sibling)
    edit.service.close()
    edit.service = RuntimeService(edit.film.fixture.duplicate_root)
    edit.service.register_timeline_commit_provider(provider("astrid"))
    persisted = json.loads(flight.read_text())

    def forbidden(*args, **kwargs):
        raise AssertionError("exact retry executed the provider")

    with monkeypatch.context() as retry:
        retry.setattr(type(edit.service._timeline_commit_provider), "commit", forbidden)
        assert edit.commit(persisted["body"], persisted["key"]) == accepted
    assert edit.document() == document
    assert edit.history() == history
    inverse = {"schema_version": 1, "op": "timeline.inverse", "version": "1", "select": None,
               "args": {"publications": [accepted["receipt"]["receipt_id"]]}}
    undone = edit.commit(edit.body([inverse]), "opaque-undo")
    for image in (document, undone["data"]["canonical_document"]):
        assert_preserved(image, materialized=image == document)
        assert_readback(edit, image)
        for sibling in ("occurrence-2", "occurrence-3"):
            assert occurrence_image(image, sibling) == occurrence_image(original, sibling)
    assert undone["data"]["canonical_document"] == original
    assert edit.document() == original
    assert_readback(edit, original)
    assert edit.history()[:-1] == history


def assert_preserved(document, *, materialized):
    occurrence, shot, internal = occurrence_image(document, "occurrence-1")
    assert_opaque(shot["payload"])
    identities = [item.get("item_id", item.get("id")) for item in shot["payload"]["items"]]
    for item, source in zip(shot["payload"]["items"], ("item-1", "item-2")):
        if materialized:
            assert item.get("item_id", item.get("id")) != source
        if "item_id" in item and "id" in item:
            assert item["item_id"] == item["id"]
        assert item["metadata"]["source_item_id"] == identities[0]
        assert item["metadata"]["source_item"] == identities[1]
        assert item["metadata"]["data"] == opaque()
        assert_opaque(item)
    for row in (document["parent_revision"]["payload"],
                document["parent_revision"]["payload"]["config"],
                document["parent_revision"]["payload"]["config"]["tracks"][0],
                document["parent_revision"]["payload"]["clips"][0], occurrence,
                internal["payload"], internal["payload"]["tracks"][0],
                internal["payload"]["clips"][0]):
        assert_opaque(row)


def test_occurrence_inverse_restores_exact_original_closure(opaque_film):
    edit, _ = opaque_film
    original = edit.document()
    command = edit.agent.move(edit.agent.bundle(original), "occurrence:occurrence-1/clip:clip-1", 0.5)
    accepted = edit.commit(edit.body([command]), "opaque-exact-move")
    inverse = {"schema_version": 1, "op": "timeline.inverse", "version": "1", "select": None,
               "args": {"publications": [accepted["receipt"]["receipt_id"]]}}
    undone = edit.commit(edit.body([inverse]), "opaque-exact-undo")
    assert undone["data"]["canonical_document"] == original
    assert edit.document() == original
