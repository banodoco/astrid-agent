"""Native visualize reads of saved Release 13 transition and phase identifiers."""
from __future__ import annotations

import copy

import pytest

from tests.editing_conformance.test_commit_runtime_m1 import open_client
from tests.helpers.trusted_seed import seed_trusted_parent
from tests.integrations.test_c06_public_timeline_visualizer import _png
from tests.sdk.test_legacy_transition_commit import PHASE_EFFECTS


def _history(service, project):
    return service.list_timeline_commits(project, "main", authorize=lambda: None)


def _seed_r13(service, project, image_path, *, transition=None, effects=None):
    _png(image_path)
    media_id = service.ingest(
        project, image_path.read_bytes(), original_name="legacy.png", media_type="image/png",
        idempotency_key="legacy-image",
    )["data"]["object_id"]
    registry = {"assets": {"still": {"media_id": media_id, "type": "image",
                                       "resolution": "96x54",
                                       "content_sha256": media_id.removeprefix("sha256:")}}}
    track = {"id": "V1", "kind": "visual", "label": "Picture"}
    transition_clip = {"id": "incoming", "track": "picture", "clipType": "image",
                       "asset": "still", "at": 0.6, "hold": 1.0}
    if transition is not None:
        transition_clip["transition"] = copy.deepcopy(transition)
    if effects is not None:
        transition_clip.update(copy.deepcopy(effects))
    clips = [{"id": "outgoing", "track": "V1", "clipType": "image",
              "asset": "still", "at": 0, "hold": 1.0},
             {**transition_clip, "track": "V1"}]
    seed_trusted_parent(service, {
        "project_id": project, "timeline_id": "main", "parent_revision_id": "r13-parent",
        "parent_composition": {
            "config": {"output": {"file": "out.mp4", "fps": 30, "resolution": "128x72"},
                       "tracks": [track], "clips": clips},
            "registry": registry, "clips": clips, "occurrences": [],
        },
        "shot_revisions": [], "internal_timeline_revisions": [],
    }, idempotency_key="r13-visualize-seed")
    return media_id


@pytest.fixture
def native_timeline(real_runtime, tmp_path):
    daemon = real_runtime(authoring_profile="astrid")
    service = daemon.service
    project = service.create_project({"slug": "legacy-visualize", "name": "Legacy visualize"})["id"]
    service.create_timeline(project, "main", idempotency_key="birth")
    return service, open_client(daemon), project, tmp_path


@pytest.mark.integration
@pytest.mark.parametrize("identity", ["crossfade", "wipe", "slide-push", "zoom-through"])
def test_native_visualize_reads_r13_incoming_transitions_without_writes(native_timeline, identity):
    service, client, project, tmp_path = native_timeline
    authored = {"type": identity, "duration": 0.4}
    _seed_r13(service, project, tmp_path / "still.png", transition=authored)
    before = _history(service, project)
    saved = service.get_project_parent_composition_revision(project, "main", "r13-parent")

    result = client.timelines.visualize(project, "main", mode="inputs", options={"detail": True})

    assert result.ok, result.to_dict()
    inspection = client.timelines.open_composition(project, "main")
    assert inspection.ok, inspection.to_dict()
    clips = inspection.data["clips"]
    incoming = next(row for row in clips if row.get("clip_id") == "incoming")
    projection = incoming["transition_projection"]
    assert projection["status"] == "resolved", projection
    assert projection["start_frame"] == 6 and projection["end_frame"] == 18, projection
    projected_id = projection.get("transition", {}).get("type")
    assert projected_id == ("cross-fade" if identity == "crossfade" else identity), projection
    assert incoming["render_timing"]["transition"] == authored
    assert service.get_project_parent_composition_revision(project, "main", "r13-parent") == saved
    assert _history(service, project) == before


@pytest.mark.integration
@pytest.mark.parametrize("slot,identity", PHASE_EFFECTS)
def test_native_visualize_reads_r13_phase_effects_as_saved_declarations(native_timeline, slot, identity):
    service, client, project, tmp_path = native_timeline
    phase = {"type": identity, "intensity": 0.5} if slot == "continuous" else {"type": identity, "duration": 0.4}
    _seed_r13(service, project, tmp_path / "still.png", effects={slot: phase})
    before = _history(service, project)
    saved = service.get_project_parent_composition_revision(project, "main", "r13-parent")

    result = client.timelines.visualize(project, "main", mode="inputs", options={"detail": True})

    assert result.ok, result.to_dict()
    inspection = client.timelines.open_composition(project, "main")
    assert inspection.ok, inspection.to_dict()
    incoming = next(row for row in inspection.data["clips"] if row.get("clip_id") == "incoming")
    assert incoming["authored_fields"][slot] == phase
    assert service.get_project_parent_composition_revision(project, "main", "r13-parent") == saved
    assert _history(service, project) == before


@pytest.mark.integration
def test_native_visualize_keeps_unknown_transition_explicitly_unresolved(native_timeline):
    service, client, project, tmp_path = native_timeline
    authored = {"type": "crossfadee", "duration": 0.4}
    _seed_r13(service, project, tmp_path / "still.png", transition=authored)
    before = _history(service, project)

    result = client.timelines.visualize(project, "main", mode="inputs", options={"detail": True})

    assert result.ok, result.to_dict()
    inspection = client.timelines.open_composition(project, "main")
    assert inspection.ok, inspection.to_dict()
    incoming = next(row for row in inspection.data["clips"] if row.get("clip_id") == "incoming")
    assert incoming["transition_projection"]["status"] == "unresolved"
    assert incoming["transition_projection"]["transition"] == authored
    assert _history(service, project) == before


@pytest.mark.integration
@pytest.mark.parametrize("identity", ["crossfade", "wipe", "slide-push", "zoom-through"])
def test_runtime_certifies_authored_r13_transition_and_readers_project_legacy_alias(native_timeline, identity):
    from runtime_protocol.capture_validation import validate_capture

    from astrid.core.timeline.reader_projection import check_reader_projection, reader_view
    from astrid.sdk.timeline_filmstrip import prepare_filmstrip

    service, client, project, tmp_path = native_timeline
    authored = {"type": identity, "duration": 0.4}
    _seed_r13(service, project, tmp_path / "still.png", transition=authored)
    saved = service.get_project_parent_composition_revision(project, "main", "r13-parent")
    snapshot = prepare_filmstrip(
        {"timeline_ref": "main", "revision_id": "r13-parent", "composed_capture": True},
        project=project, client=client,
    )["capture_snapshot"]
    ref = {
        "source_kind": "revision",
        "project_id": project,
        "timeline_id": "main",
        "revision_id": "r13-parent",
        "revision_digest": saved["content_digest"],
    }

    # Runtime must certify the SDK's exact saved capture, including its authored ID.
    assert validate_capture(service, snapshot, ref, {})
    incoming = next(row for row in snapshot["config"]["clips"] if row["id"] == "incoming")
    assert incoming["transition"] == authored

    original_config = copy.deepcopy(snapshot["config"])
    original_registry = copy.deepcopy(snapshot["registry"])
    projected_config, projected_registry = reader_view(snapshot["config"], snapshot["registry"])
    check_reader_projection(projected_config, projected_registry)
    projected_incoming = next(row for row in projected_config["clips"] if row["id"] == "incoming")
    expected = {**authored, "type": "cross-fade"} if identity == "crossfade" else authored
    assert projected_incoming["transition"] == expected
    assert snapshot["config"] == original_config
    assert snapshot["registry"] == original_registry
