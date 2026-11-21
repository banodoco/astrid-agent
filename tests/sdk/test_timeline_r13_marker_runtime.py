"""R14 readers reopen the R13 open track-app extension without changing storage."""
from __future__ import annotations

import base64
import copy

import pytest

from tests.editing_conformance.test_commit_runtime_m1 import open_client
from tests.helpers.trusted_seed import seed_trusted_parent

pytestmark = pytest.mark.integration

_MARKER = "clipPlacementVersion"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+aRfsAAAAASUVORK5CYII="
)


@pytest.fixture
def realm(real_runtime):
    daemon = real_runtime(authoring_profile="astrid")
    service = daemon.service
    project = service.create_project({"slug": "f7-reader", "name": "F7 reader"})["id"]
    service.create_timeline(project, "main", idempotency_key="birth")
    return service, open_client(daemon), project


def _history(service, project):
    return service.list_timeline_commits(project, "main", authorize=lambda: None)


def _patch(service, project, change, key):
    head = _history(service, project)["head"]
    body = {
        "api_version": 1, "project_id": project, "timeline_id": "main",
        "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
        "commands": [{"schema_version": 1, "op": "doc.patch", "version": "1",
                      "select": None, "args": {"changes": [change]}}],
        "label": "Update track app", "merge": "fields", "reads": [],
        "steps": [{"id": "track-app", "label": "Track app", "intent": "Update track app",
                   "tags": [], "command_ordinals": [0], "patch": [change]}],
    }
    return service.commit_timeline(
        project, "main", body, idempotency_key=key, publisher="owner",
        invocation_path=f"/v1/projects/{project}/timelines/main/commit", authorize=lambda: None,
    )


@pytest.mark.parametrize("reader", ["render", "visualize"])
def test_saved_r13_marker_is_absent_only_in_reader_projection(realm, reader):
    from astrid.core.timeline.reader_projection import check_reader_projection, reader_view
    from astrid.packs.rendering.actions.render.managed_timeline import (
        resolve_managed_render_snapshot,
        validate_managed_render_snapshot,
    )
    from astrid.sdk.timeline.commit import create_runtime_commit_service, reader_problem
    from astrid.sdk.timeline_filmstrip import prepare_filmstrip
    from runtime_protocol.capture_validation import validate_capture

    service, client, project = realm
    object_id = service.ingest(
        project, _PNG, original_name="still.png", media_type="image/png", idempotency_key="still",
    )["data"]["object_id"]
    track = {"id": "V1", "label": "Picture", "kind": "visual",
             "app": {_MARKER: 2, "org.example": {"keep": True}}}
    # The accepted R13 shape used the open track app bag. Reproduce that
    # stored input through Runtime's trusted fixture boundary.
    seed_trusted_parent(service, {
        "project_id": project, "timeline_id": "main", "parent_revision_id": "r13-parent",
        "parent_composition": {
            "config": {"output": {"file": "out.mp4", "fps": 30, "resolution": "128x72"},
                       "tracks": [track]},
            "registry": {"assets": {"still": {"media_id": object_id, "type": "image",
                                             "content_sha256": object_id.removeprefix("sha256:")}}},
            "clips": [{"id": "still", "track": "V1", "clipType": "image", "asset": "still",
                       "at": 0, "hold": 1}],
            "occurrences": [],
        },
    }, idempotency_key="r13-seed")
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
    assert config["clips"][0]["asset"] == "still"
    assert config["tracks"][0]["app"].get(_MARKER) != 2
    assert config["tracks"][0]["app"]["org.example"] == {"keep": True}
    assert service.get_project_parent_composition_revision(project, "main", "r13-parent") == revision
    assert _history(service, project) == history
    assert revision["payload"]["config"]["tracks"] == [track]
    provider = create_runtime_commit_service()
    document = history["items"][-1]["receipt"]["result"]["canonical_document"]
    assert reader_problem(provider, document) is None

    updated = copy.deepcopy(track["app"])
    updated["org.example"]["keep"] = False
    change = {"op": "set", "ref": "parent", "path": ["config", "tracks", "id:V1", "app"],
              "before": {"state": "present", "value": track["app"]}, "value": updated}
    accepted = _patch(service, project, change, "edit")
    assert accepted["data"]["status"] == "accepted"
    new_parent = accepted["data"]["canonical_document"]["parent_revision"]["payload"]
    assert new_parent["config"]["tracks"][0]["app"] == updated
    records = _history(service, project)["items"][-1]["records"]
    assert records["steps"][0]["patch"] == [change]


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
