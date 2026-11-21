"""R14 readers reopen the R13 open track-app extension without changing storage."""
from __future__ import annotations

import copy

import pytest

from tests.editing_conformance.test_commit_runtime_m1 import open_client
from tests.helpers.trusted_seed import seed_trusted_parent
from tests.integrations.test_c06_public_timeline_visualizer import _png

pytestmark = pytest.mark.integration

_MARKER = "clipPlacementVersion"


@pytest.fixture
def realm(real_runtime):
    daemon = real_runtime(authoring_profile="astrid")
    service = daemon.service
    project = service.create_project({"slug": "f7-reader", "name": "F7 reader"})["id"]
    service.create_timeline(project, "main", idempotency_key="birth")
    return service, open_client(daemon), project


def _history(service, project):
    return service.list_timeline_commits(project, "main", authorize=lambda: None)


def _patch(service, project, change, key, *, reads=()):
    head = _history(service, project)["head"]
    body = {
        "api_version": 1, "project_id": project, "timeline_id": "main",
        "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
        "commands": [{"schema_version": 1, "op": "doc.patch", "version": "1",
                      "select": None, "args": {"changes": [change]}}],
        "label": "Guarded edit", "merge": "fields", "reads": list(reads),
        "steps": [{"id": "edit", "label": "Edit", "intent": "Guarded edit",
                   "tags": [], "command_ordinals": [0], "patch": [change]}],
    }
    return service.commit_timeline(
        project, "main", body, idempotency_key=key, publisher="owner",
        invocation_path=f"/v1/projects/{project}/timelines/main/commit", authorize=lambda: None,
    )


def seed_r13_timeline(service, project, path, *, shot_id="shot"):
    _png(path)
    object_id = service.ingest(
        project, path.read_bytes(), original_name="still.png", media_type="image/png", idempotency_key="still",
    )["data"]["object_id"]
    track = {"id": "V1", "label": "Picture", "kind": "visual",
             "app": {_MARKER: 2, "org.example": {"keep": True}}}
    registry = {"assets": {"still": {"media_id": object_id, "type": "image", "resolution": "96x54",
                                     "content_sha256": object_id.removeprefix("sha256:")}}}
    # R13's open track app bag accepted these markers in both timeline scopes.
    # Use Runtime's trusted fixture boundary to reproduce the stored shape.
    seed_trusted_parent(service, {
        "project_id": project, "timeline_id": "main", "parent_revision_id": "r13-parent",
        "parent_composition": {
            "config": {"output": {"file": "out.mp4", "fps": 30, "resolution": "128x72"},
                       "tracks": [track]},
            "registry": registry,
            "clips": [{"id": "still", "track": "V1", "clipType": "image", "asset": "still",
                       "at": 0, "hold": 1}],
            "occurrences": [
                {"occurrence_id": "first-shot", "shot_id": shot_id, "shot_revision_id": "r13-shot",
                 "placement": {"start_ms": 0, "track": "V1"},
                 "source_offset": {"start": 0, "end": 1000}, "duration_ms": 1000,
                 "speed": {"numerator": 1, "denominator": 1}, "track": "V1"},
            ],
        },
        "shot_revisions": [
            {"shot_id": shot_id, "revision_id": "r13-shot", "internal_timeline_revision_id": "r13-child",
             "payload": {"name": "Shot", "items": [], "text_bindings": []}},
        ],
        "internal_timeline_revisions": [
            {"timeline_id": "main", "revision_id": "r13-child",
             "payload": {"output": {"file": "child.mp4", "fps": 30, "resolution": "96x54"},
                         "tracks": [{**track, "id": "picture"}],
                         "clips": [{"id": "child-still", "track": "picture", "clipType": "image",
                                    "asset": "still", "at": 0, "hold": 1}],
                         "registry": registry}},
        ],
    }, idempotency_key="r13-seed")
    return track


@pytest.mark.parametrize("reader", ["render", "visualize"])
def test_saved_r13_marker_is_absent_only_in_reader_projection(realm, reader, tmp_path):
    from astrid.core.timeline.reader_projection import check_reader_projection, reader_view
    from astrid.packs.rendering.actions.render.managed_timeline import (
        resolve_managed_render_snapshot,
        validate_managed_render_snapshot,
    )
    from astrid.sdk.timeline.commit import create_runtime_commit_service, reader_problem
    from astrid.sdk.timeline_filmstrip import prepare_filmstrip
    from runtime_protocol.capture_validation import validate_capture
    from runtime_protocol.util import canonical_json

    service, client, project = realm
    track = seed_r13_timeline(service, project, tmp_path / "still.png")
    revision = service.get_project_parent_composition_revision(project, "main", "r13-parent")
    history = _history(service, project)
    if reader == "render":
        snapshot = resolve_managed_render_snapshot(project_ref=project, timeline_ref="main", client=client)
        validate_managed_render_snapshot(snapshot)
        assert snapshot.head_hash == revision["content_digest"].removeprefix("sha256:")
        config = snapshot.config
    else:
        snapshot = prepare_filmstrip(
            {"timeline_ref": "main", "composed_capture": True}, project=project, client=client,
        )["capture_snapshot"]
        config = snapshot["config"]
        check_reader_projection(*reader_view(config, snapshot["registry"]))
        assert validate_capture(service, snapshot, {
            "source_kind": "revision", "project_id": project, "timeline_id": "main",
            "revision_id": "r13-parent", "revision_digest": revision["content_digest"],
        }, {})
    assert {clip["asset"] for clip in config["clips"]} == {"still", "first-shot:still"}
    assert {row["id"] for row in config["tracks"]} == {"V1", "first-shot~picture"}
    for row in config["tracks"]:
        assert row["app"].get(_MARKER) != 2
        assert row["app"]["org.example"] == {"keep": True}
    assert service.get_project_parent_composition_revision(project, "main", "r13-parent") == revision
    assert _history(service, project) == history
    assert revision["payload"]["config"]["tracks"] == [track]
    child = service.get_project_timeline_revision(project, "main", "r13-child")
    assert child["payload"]["tracks"][0]["app"][_MARKER] == 2
    provider = create_runtime_commit_service()
    document = history["items"][-1]["receipt"]["result"]["canonical_document"]
    assert reader_problem(provider, document) is None

    guard = {"kind": "field", "ref": "parent", "path": ["config", "tracks", "id:V1", "app", _MARKER],
             "state": {"state": "present", "value": 2}}
    change = {"op": "set", "ref": "parent", "path": ["meta"],
              "before": {"state": "absent"}, "value": {"org.example.timeline": {"note": "Keep"}}}
    accepted = _patch(service, project, change, "edit", reads=[guard])
    assert accepted["data"]["status"] == "accepted"
    new_document = accepted["data"]["canonical_document"]
    new_tracks = new_document["parent_revision"]["payload"]["config"]["tracks"]
    assert canonical_json(new_tracks) == canonical_json(revision["payload"]["config"]["tracks"])
    assert canonical_json(new_document["internal_timeline_revisions"][0]["payload"]) == canonical_json(child["payload"])
    records = _history(service, project)["items"][-1]["records"]
    assert records["steps"][0]["patch"] == [change]
    assert guard in records["steps"][0]["dependencies"]
    assert service.get_project_parent_composition_revision(project, "main", "r13-parent") == revision
    assert service.get_project_timeline_revision(project, "main", "r13-child") == child


def test_authored_v2_marker_is_refused_without_writes(realm):
    from runtime_protocol.errors import RuntimeErrorBase

    service, _client, project = realm
    history = _history(service, project)
    original = service.get_project_parent_composition_revision(
        project, "main", history["head"]["revision_id"],
    )
    tracks = original["payload"]["config"]["tracks"]
    updated = copy.deepcopy(tracks)
    updated[0].setdefault("app", {})[_MARKER] = 2
    with pytest.raises(RuntimeErrorBase, match=r"app\.clipPlacementVersion"):
        _patch(service, project, {
            "op": "set", "ref": "parent", "path": ["config", "tracks"],
            "before": {"state": "present", "value": tracks}, "value": updated,
        }, "rejected")
    assert _history(service, project) == history


@pytest.mark.parametrize("change_fit", [False, True], ids=["marker-only", "fit"])
def test_guarded_placement_edit_on_legacy_track_requires_version_one(realm, tmp_path, change_fit):
    from runtime_protocol.errors import RuntimeErrorBase

    service, _client, project = realm
    seed_r13_timeline(service, project, tmp_path / "still.png")
    history = _history(service, project)
    revision = service.get_project_parent_composition_revision(project, "main", "r13-parent")
    tracks = revision["payload"]["config"]["tracks"]
    proposed = copy.deepcopy(tracks)
    if change_fit:
        proposed[0]["fit"] = "cover"
    del proposed[0]["app"][_MARKER]
    change = {"op": "set", "ref": "parent", "path": ["config", "tracks"],
              "before": {"state": "present", "value": tracks}, "value": proposed}
    with pytest.raises(RuntimeErrorBase, match=r"app\.clipPlacementVersion"):
        _patch(service, project, change, "without-version")
    assert _history(service, project) == history

    proposed[0]["app"][_MARKER] = 1
    accepted = _patch(service, project, change, "canonical-version")
    assert accepted["data"]["status"] == "accepted"
    parent = accepted["data"]["canonical_document"]["parent_revision"]["payload"]
    assert parent["config"]["tracks"] == proposed
    assert service.get_project_parent_composition_revision(project, "main", "r13-parent") == revision


def share_r13_child(service, client, project, outbox):
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared
    from astrid.sdk.timeline.sync import CommitRefused
    from runtime_protocol.util import canonical_json

    history = _history(service, project)
    before = history["items"][-1]["receipt"]["result"]["canonical_document"]
    timeline = Timeline.open(project, "main", client=client, outbox_path=outbox)
    command = capture_occurrence_place_shared(
        bundle_from_document(before), "occurrence:first-shot", "second-shot", on=1,
    )
    try:
        preview = timeline.commit([command], label="Share R13 child", dry_run=True)
    except CommitRefused as exc:
        exc.add_note(str(exc.failure))
        raise
    assert preview["data"]["would_status"] == "accepted" and preview["receipt"] is None
    assert _history(service, project) == history

    result = timeline.commit([command], label="Share R13 child", key="share-r13-child")
    after = result["data"]["canonical_document"]
    assert result["data"]["status"] == "accepted"
    assert preview["data"]["canonical_document"] == after
    for family in ("shot_revisions", "internal_timeline_revisions", "dependency_manifest"):
        assert canonical_json(after[family]) == canonical_json(before[family])
    parent_before = before["parent_revision"]["payload"]
    parent_after = after["parent_revision"]["payload"]
    assert canonical_json(parent_after["config"]["tracks"]) == canonical_json(parent_before["config"]["tracks"])
    assert [row["occurrence_id"] for row in parent_after["occurrences"]] == ["first-shot", "second-shot"]
    assert {row["shot_id"] for row in parent_after["occurrences"]} == {"chapter-shot"}
    assert {row["shot_revision_id"] for row in parent_after["occurrences"]} == {"r13-shot"}
    reopened = bundle_from_document(after)
    assert len(reopened["shots"]) == 2
    assert "chapter-shot" not in reopened["shots"]
    assert all(item["internal_timeline"]["tracks"][0]["app"][_MARKER] == 2
               for item in reopened["shots"].values())
    return timeline, before, result


def test_shared_r13_child_aliases_reopen_without_changing_the_physical_source(realm, tmp_path):
    from astrid.core.timeline.reader_projection import check_reader_projection, reader_view
    from astrid.packs.rendering.actions.render.managed_timeline import (
        resolve_managed_render_snapshot,
        validate_managed_render_snapshot,
    )
    from astrid.sdk.timeline_filmstrip import prepare_filmstrip
    from runtime_protocol.capture_validation import validate_capture
    from runtime_protocol.errors import RuntimeErrorBase

    service, client, project = realm
    seed_r13_timeline(service, project, tmp_path / "still.png", shot_id="chapter-shot")
    timeline, before, result = share_r13_child(service, client, project, tmp_path / "outbox.json")
    after = result["data"]["canonical_document"]
    parent = after["parent_revision"]
    history = _history(service, project)
    child = service.get_project_timeline_revision(project, "main", "r13-child")

    render = resolve_managed_render_snapshot(project_ref=project, timeline_ref="main", client=client)
    validate_managed_render_snapshot(render)
    assert render.head_event_id == parent["revision_id"]
    visualize = prepare_filmstrip(
        {"timeline_ref": "main", "composed_capture": True}, project=project, client=client,
    )["capture_snapshot"]
    check_reader_projection(*reader_view(visualize["config"], visualize["registry"]))
    assert validate_capture(service, visualize, {
        "source_kind": "revision", "project_id": project, "timeline_id": "main",
        "revision_id": parent["revision_id"], "revision_digest": parent["content_digest"],
    }, {})
    for config in (render.config, visualize["config"]):
        assert {row["id"] for row in config["tracks"]} == {"V1", "first-shot~picture", "second-shot~picture"}
        assert {row["id"] for row in config["clips"]} == {"still", "first-shot:child-still", "second-shot:child-still"}
        assert all(row["app"].get(_MARKER) != 2 for row in config["tracks"])

    tracks = child["payload"]["tracks"]
    updated = copy.deepcopy(tracks)
    updated[0]["scale"] = 2
    with pytest.raises(RuntimeErrorBase, match=r"app\.clipPlacementVersion"):
        _patch(service, project, {
            "op": "set", "ref": "internal-revision:r13-child", "path": ["tracks"],
            "before": {"state": "present", "value": tracks}, "value": updated,
        }, "changed-legacy-placement")
    assert _history(service, project) == history
    assert service.get_project_timeline_revision(project, "main", "r13-child") == child

    restored = timeline.undo(publications=[result["receipt"]["receipt_id"]], idempotency_key="undo-r13-share")
    assert restored["data"]["status"] == "accepted"
    assert restored["data"]["canonical_document"] == before
