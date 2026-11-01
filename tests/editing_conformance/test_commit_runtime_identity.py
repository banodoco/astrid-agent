"""Real shared-occurrence projection occupancy, atomic refusal and safe reuse.

Run with ``pytest -m integration`` against the paired Runtime checkout.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from tests.helpers.runtime import use_runtime
from tests.helpers.trusted_seed import seed_trusted_parent

pytestmark = pytest.mark.integration
REF = "occurrence:occurrence-1/clip:clip-1"


@pytest.fixture
def identity(request, tmp_path, monkeypatch):
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / "tests"))
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from test_timeline_commit_integration import Editing, rows
    from timeline_commit_film_fixture import build_film_fixture

    from astrid.sdk.timeline.capture import capture_move
    from astrid.sdk.timeline.commit import bundle_from_document

    fixture = build_film_fixture(tmp_path)
    agent = SimpleNamespace(move=capture_move, bundle=bundle_from_document)
    film = SimpleNamespace(fixture=fixture)
    service = RuntimeService(fixture.duplicate_root)
    edit = Editing(service, film, agent)
    try:
        service._timeline_commits()[0].install_verified_baseline(
            edit.project, edit.timeline, authorize=lambda: None)
        original = edit.document()
        parent = copy.deepcopy(original["parent_revision"]["payload"])
        _, source_shot, source_internal = occurrence_image(original, "occurrence-1")
        internal = {k: copy.deepcopy(source_internal[k]) for k in ("timeline_id", "revision_id", "payload")}
        shot = {k: copy.deepcopy(source_shot[k]) for k in (
            "shot_id", "revision_id", "internal_timeline_revision_id", "payload")}
        internal["revision_id"] = "identity-internal"
        internal["payload"]["clips"][0]["hold"] = 0.25
        shot.update(revision_id="identity-shot", internal_timeline_revision_id="identity-internal")
        for occurrence in parent["occurrences"][:2]:
            occurrence.update(shot_id=shot["shot_id"], shot_revision_id=shot["revision_id"])
        seed_trusted_parent(service, {
            "project_id": edit.project, "timeline_id": edit.timeline,
            "expected_head": fixture.parent_revision_id, "parent_revision_id": "identity-baseline",
            "parent_composition": parent, "internal_timeline_revisions": [internal],
            "shot_revisions": [shot],
        }, idempotency_key="identity-baseline")
        bundle = agent.bundle(edit.document())
        destinations = {p["occurrence_id"]: p["shot_id"] for p in bundle["placements"]}
        destination = destinations["occurrence-1"]
        assert destination.startswith("shot-placement-")
        assert destination not in {shot["shot_id"], destinations["occurrence-2"]}
        foreign = service.create_project({"slug": "foreign", "name": "Foreign"},
                                         idempotency_key="foreign")["id"]
        timelines = [(edit.project, edit.timeline)]
        # Seed valid unrelated films before activating the command provider.
        for project, tag in ((edit.project, "neighbour"), (foreign, "foreign")):
            timeline = tag + "-film"
            occupied = request.param == ("same-project" if project == edit.project else "other-project")
            shot_id = destination if occupied else tag + "-shot"
            service.create_timeline(project, timeline, name=tag, idempotency_key=tag + "-timeline")
            media = service.ingest(project, tag.encode(), original_name=tag + ".mp4",
                                   media_type="video/mp4", idempotency_key=tag + "-media")["data"]["object_id"]
            other_internal, other_shot = copy.deepcopy(internal), copy.deepcopy(shot)
            other_internal.update(timeline_id=timeline, revision_id=tag + "-internal")
            other_internal["payload"]["registry"] = {"assets": {"picture-1": {
                "object_id": media, "type": "video"}}}
            other_internal["payload"]["clips"][0].pop("elementRef", None)
            other_shot.update(shot_id=shot_id, revision_id=tag + "-revision",
                              internal_timeline_revision_id=other_internal["revision_id"])
            other_shot["payload"].update(name=tag, metadata={"opaque": [None, {"keep": tag}]},
                text_bindings=[], provenance={"source_media": media}, items=[{
                    "item_id": tag + f"-item-{index}", "media_id": media, "source_frame": index,
                    "metadata": {"opaque": [None, {"keep": index}]},
                } for index in range(2)])
            other_parent = copy.deepcopy(parent)
            other_parent.update(clips=[], registry={"assets": {}}, occurrences=[{
                **copy.deepcopy(parent["occurrences"][0]), "occurrence_id": "unrelated",
                "shot_id": shot_id, "shot_revision_id": other_shot["revision_id"],
            }])
            seed_trusted_parent(service, {
                "project_id": project, "timeline_id": timeline, "expected_head": None,
                "parent_revision_id": tag + "-parent", "parent_composition": other_parent,
                "internal_timeline_revisions": [other_internal], "shot_revisions": [other_shot],
            }, idempotency_key=tag + "-publication")
            timelines.append((project, timeline))
        service.register_timeline_commit_provider(load_timeline_commit_provider("astrid"))
        yield SimpleNamespace(edit=edit, rows=rows, destination=destination, timelines=timelines,
                              mode=request.param, provider=load_timeline_commit_provider)
    finally:
        edit.service.close()


def occurrence_image(document, occurrence_id):
    occurrence = next(p for p in document["parent_revision"]["payload"]["occurrences"]
                      if p["occurrence_id"] == occurrence_id)
    shot = next(s for s in document["shot_revisions"]
                if s["revision_id"] == occurrence["shot_revision_id"])
    internal = next(i for i in document["internal_timeline_revisions"]
                    if i["revision_id"] == shot["internal_timeline_revision_id"])
    return occurrence, shot, internal


def world(case):
    service = case.edit.service
    result = {}
    for project, timeline in case.timelines:
        head = service.get_project_timeline(project, timeline)
        history = service.list_timeline_commits(project, timeline, authorize=lambda: None)
        result[project, timeline] = (head, history, [
            service._timeline_commits()[0].revision_closure(project, timeline, entry["revision_id"])
            for entry in history["items"]])
    # Complete rows include mutable metadata/items/heads and every revision,
    # event, receipt, retained input and sequence counter, not just row counts.
    projections = {row["id"]: (dict(row), [dict(item) for item in service.store.conn.execute(
        "SELECT * FROM shot_items WHERE shot_id=? ORDER BY id", (row["id"],))],
        [dict(head) for head in service.store.conn.execute(
            "SELECT * FROM shot_revision_heads WHERE shot_id=?", (row["id"],))])
        for row in service.store.conn.execute("SELECT * FROM project_shots ORDER BY id")}
    return result, case.rows(service), projections


def move_body(case, on):
    edit = case.edit
    return edit.body([edit.agent.move(edit.agent.bundle(edit.document()), REF, on)])


@pytest.mark.parametrize("identity", ["same-project", "other-project"], indirect=True)
def test_occupied_shared_occurrence_destination_refuses_atomically(identity):
    from runtime_protocol.errors import RuntimeErrorBase

    edit = identity.edit
    occupant = edit.service.store.conn.execute(
        "SELECT * FROM project_shots WHERE id=?", (identity.destination,),
    ).fetchone()
    assert occupant is not None
    assert len(edit.service.store.conn.execute(
        "SELECT * FROM shot_items WHERE shot_id=?", (identity.destination,),
    ).fetchall()) == 2
    before = world(identity)
    for dry_run in (True, False):
        body = {**move_body(identity, 0.5), "dry_run": dry_run}
        try:
            edit.commit(body, "occupied-dry" if dry_run else "occupied-move")
        except RuntimeErrorBase as error:
            assert error.status == 409 and error.code == "conflict"
            if identity.mode == "same-project":
                assert "destination" in str(error).lower() and "occupied" in str(error).lower()
        else:
            if not dry_run:
                assert world(identity) == before, "accepted move overwrote an unrelated projection"
                pytest.fail("accepted shared-occurrence move onto an unrelated occupied destination")
        assert world(identity) == before


@pytest.mark.parametrize("identity", ["fresh"], indirect=True)
def test_fresh_shared_occurrence_retry_own_move_and_historical_restore(identity, tmp_path, monkeypatch):
    from runtime_protocol.service import RuntimeService

    edit = identity.edit
    original = edit.document()
    baseline = world(identity)
    body = move_body(identity, 0.5)
    preview = edit.commit({**body, "dry_run": True}, "fresh-dry")
    assert preview["data"]["status"] == "preview"
    assert world(identity) == baseline
    flight = tmp_path / "flight.json"
    flight.write_text(json.dumps({"key": "lost-ack", "body": body}))
    accepted = None

    def lose_ack():
        nonlocal accepted
        accepted = edit.commit(body, "lost-ack")
        raise ConnectionError("lost acknowledgement after durable acceptance")

    with pytest.raises(ConnectionError, match="lost acknowledgement"):
        lose_ack()
    assert accepted["data"]["status"] == "accepted"
    first = accepted["data"]["canonical_document"]
    moved, _, internal = occurrence_image(first, "occurrence-1")
    assert moved["shot_id"] == identity.destination
    assert internal["payload"]["clips"][0]["at"] == 0.5
    for sibling in ("occurrence-2", "occurrence-3"):
        assert occurrence_image(first, sibling) == occurrence_image(original, sibling)
    after = world(identity)
    assert {k: after[2][k] for k in baseline[2]} == baseline[2]
    assert {k: v for k, v in after[0].items() if k != (edit.project, edit.timeline)} == {
        k: v for k, v in baseline[0].items() if k != (edit.project, edit.timeline)}
    edit.assert_readback(accepted)
    edit.service.close()
    edit.service = RuntimeService(edit.film.fixture.duplicate_root)
    edit.service.register_timeline_commit_provider(identity.provider("astrid"))
    restarted = world(identity)
    assert restarted[0] == after[0] and restarted[2] == after[2]

    def forbidden(*args, **kwargs):
        raise AssertionError("exact retry executed provider or projection persistence")

    persisted = json.loads(flight.read_text())
    with monkeypatch.context() as retry:
        retry.setattr(type(edit.service._timeline_commit_provider), "commit", forbidden)
        retry.setattr(type(edit.service._timeline_commits()[0]), "_persist_closure", forbidden)
        assert edit.commit(persisted["body"], persisted["key"]) == accepted
    assert world(identity) == restarted
    # Opening an unshared sibling uses its ACTUAL pinned source identity, not
    # the virtual authoring slot it had while both occurrences shared a shot.
    bundle = edit.agent.bundle(edit.document())
    sibling = occurrence_image(first, "occurrence-2")
    assert next(p for p in bundle["placements"] if p["occurrence_id"] == "occurrence-2")["shot_id"] == sibling[0]["shot_id"]
    own = edit.commit(move_body(identity, 0.75), "own-subsequent-move")
    own_document = own["data"]["canonical_document"]
    assert occurrence_image(own_document, "occurrence-1")[0]["shot_id"] == identity.destination
    assert occurrence_image(own_document, "occurrence-1")[2]["payload"]["clips"][0]["at"] == 0.75
    assert occurrence_image(own_document, "occurrence-2") == sibling
    restore = {"schema_version": 1, "op": "timeline.restore", "version": "1", "select": None,
               "args": {"revision_id": original["parent_revision"]["revision_id"],
                        "content_digest": original["parent_revision"]["content_digest"]}}
    restored = edit.commit(edit.body([restore]), "historical-restore")
    assert restored["data"]["canonical_document"]["parent_revision"]["payload"] == original["parent_revision"]["payload"]
    rematerialized = edit.commit(move_body(identity, 0.5), "historical-rematerialization")
    assert occurrence_image(rematerialized["data"]["canonical_document"], "occurrence-1") == occurrence_image(first, "occurrence-1")
    for result in (accepted, own, restored, rematerialized):
        edit.assert_readback(result)
    final = world(identity)
    _, old_history, old_closures = baseline[0][edit.project, edit.timeline]
    assert final[0][edit.project, edit.timeline][1]["items"][:len(old_history["items"])] == old_history["items"]
    assert final[0][edit.project, edit.timeline][2][:len(old_closures)] == old_closures
    assert {k: final[2][k] for k in baseline[2]} == baseline[2]
    assert {k: v for k, v in final[0].items() if k != (edit.project, edit.timeline)} == {
        k: v for k, v in baseline[0].items() if k != (edit.project, edit.timeline)}
