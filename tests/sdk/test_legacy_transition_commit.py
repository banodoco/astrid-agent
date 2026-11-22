"""R13-authored transitions and phase effects survive real Runtime commits."""
from __future__ import annotations

import copy

import pytest

from tests.editing_conformance.test_commit_runtime_m1 import open_client
from tests.helpers.trusted_seed import seed_trusted_parent

pytestmark = [pytest.mark.integration, pytest.mark.timeout(45)]

TRANSITIONS = ("crossfade", "wipe", "slide-push", "zoom-through", "fade")
PHASE_EFFECTS = (
    ("entrance", "fade"), ("entrance", "slide-left"), ("entrance", "slide-right"),
    ("entrance", "slide-up"), ("entrance", "slide-down"), ("entrance", "zoom-in"),
    ("entrance", "zoom-spin"), ("entrance", "pulse"), ("entrance", "flip"),
    ("entrance", "bounce"), ("entrance", "meteorite"),
    ("exit", "slide-down"), ("exit", "zoom-out"), ("exit", "flip"),
    ("exit", "fade-out"), ("exit", "shrink"), ("exit", "dissolve"),
    ("continuous", "ken-burns"), ("continuous", "float"), ("continuous", "glitch"),
    ("continuous", "slow-zoom"), ("continuous", "drift"),
)


@pytest.fixture
def seeded(real_runtime, tmp_path):
    daemon = real_runtime(authoring_profile="astrid")
    service = daemon.service
    project = service.create_project({"slug": "r13-effects", "name": "R13 effects"})["id"]
    service.create_timeline(project, "main", idempotency_key="birth")
    image = tmp_path / "still.png"
    # Small valid PNG used only to establish a real media reference.
    from tests.integrations.test_c06_public_timeline_visualizer import _png
    _png(image)
    media = service.ingest(project, image.read_bytes(), original_name="still.png", media_type="image/png",
                           idempotency_key="still")["data"]["object_id"]
    registry = {"assets": {"still": {"media_id": media, "type": "image", "resolution": "96x54",
                                        "content_sha256": media.removeprefix("sha256:")}}}
    seed_trusted_parent(service, {
        "project_id": project, "timeline_id": "main", "parent_revision_id": "r13-parent",
        "parent_composition": {
            "config": {"output": {"file": "out.mp4", "fps": 30, "resolution": "128x72"},
                       "tracks": [{"id": "V1", "kind": "visual"}]},
            "registry": registry,
            "clips": [
                {"id": "legacy", "track": "V1", "clipType": "image", "asset": "still", "at": 0, "hold": 1},
                {"id": "neighbor", "track": "V1", "clipType": "image", "asset": "still", "at": 1, "hold": 1},
            ],
            "occurrences": [],
        }, "shot_revisions": [], "internal_timeline_revisions": [],
    }, idempotency_key="seed-r13")
    return service, open_client(daemon), project


def _history(service, project):
    return service.list_timeline_commits(project, "main", authorize=lambda: None)


def _legacy_clip(service, project):
    history = _history(service, project)
    document = history["items"][-1]["receipt"]["result"]["canonical_document"]
    return copy.deepcopy(document["parent_revision"]["payload"]["clips"][0])


def _commit_position(service, client, project):
    from astrid.sdk import timeline

    tl = timeline.open(project, "main", client=client)
    tl.move("parent/clip:legacy", 1)
    return tl.commit("Move legacy clip", key="move-legacy")


def _patch_transition(service, project, before, after, key):
    head = _history(service, project)["head"]
    change = {"op": "set", "ref": "parent",
              "path": ["clips", "id:legacy", "transition"],
              "before": {"state": "absent"} if before is None else {"state": "present", "value": before},
              "value": after}
    body = {
        "api_version": 1, "project_id": project, "timeline_id": "main",
        "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
        "commands": [{"schema_version": 1, "op": "doc.patch", "version": "1",
                      "select": None, "args": {"changes": [change]}}],
        "label": "Set transition", "merge": "fields", "reads": [],
        "steps": [{"id": "edit", "label": "Set transition", "intent": "Set transition",
                   "tags": [], "command_ordinals": [0], "patch": [change]}],
    }
    return service.commit_timeline(
        project, "main", body, idempotency_key=key, publisher="owner",
        invocation_path=f"/v1/projects/{project}/timelines/main/commit", authorize=lambda: None,
    )


def test_readable_timeline_refuses_introduced_crossfadee(seeded):
    from runtime_protocol.errors import RuntimeErrorBase

    service, _client, project = seeded
    before = _history(service, project)
    with pytest.raises(RuntimeErrorBase, match="crossfadee"):
        _patch_transition(service, project, None, {"type": "crossfadee", "duration": 0.25}, "bad-transition")
    assert _history(service, project) == before


@pytest.mark.parametrize("identity", ["wipe", "slide-push", "zoom-through"])
def test_readable_timeline_admits_legacy_built_in_on_transition_edit(seeded, identity):
    service, _client, project = seeded
    transition = {"type": identity, "duration": 0.25}
    result = _patch_transition(service, project, None, transition, "set-legacy-transition")
    assert result["data"]["status"] == "accepted"
    assert _legacy_clip(service, project)["transition"] == transition


def test_explicit_crossfade_canonicalization_commits_without_alias(seeded):
    service, _client, project = seeded
    parent_id = _history(service, project)["head"]["revision_id"]
    payload = copy.deepcopy(service.get_project_parent_composition_revision(project, "main", parent_id)["payload"])
    payload["clips"][0]["transition"] = {"type": "crossfade", "duration": 0.25}
    _replace_seed_parent(service, project, payload, "seed-crossfade-alias")
    before = _history(service, project)
    result = _patch_transition(service, project, {"type": "crossfade", "duration": 0.25},
                               {"type": "cross-fade", "duration": 0.25}, "canonical-transition")
    assert result["data"]["status"] == "accepted"
    saved = result["data"]["canonical_document"]["parent_revision"]["payload"]["clips"][0]
    assert saved["transition"] == {"type": "cross-fade", "duration": 0.25}
    assert len(_history(service, project)["items"]) == len(before["items"]) + 1


@pytest.mark.parametrize("transition", TRANSITIONS)
def test_r13_transition_bytes_and_history_survive_position_commit(seeded, transition):
    service, client, project = seeded
    before = _history(service, project)
    original = _legacy_clip(service, project)
    original["transition"] = {"type": transition, "duration": 0.25}
    # Seed setup is intentionally trusted; the next commit is compiled and accepted by Agent.
    current = service.get_project_parent_composition_revision(project, "main", "r13-parent")
    payload = copy.deepcopy(current["payload"])
    payload["clips"][0] = original
    _replace_seed_parent(service, project, payload, "seed-" + transition)
    seeded_history = _history(service, project)
    parent_id = _history(service, project)["head"]["revision_id"]
    seeded_revision = service.get_project_parent_composition_revision(project, "main", parent_id)
    result = _commit_position(service, client, project)
    assert result["data"]["status"] == "accepted"
    saved = result["data"]["canonical_document"]["parent_revision"]["payload"]["clips"][0]
    assert saved["transition"] == {"type": transition, "duration": 0.25}
    assert saved["at"] == 1
    assert service.get_project_parent_composition_revision(project, "main", parent_id) == seeded_revision
    after = _history(service, project)
    assert len(after["items"]) == len(seeded_history["items"]) + 1
    assert after["items"][-1]["records"]["steps"][0]["command_ordinals"] == [0]
    assert before["head"] != after["head"]


@pytest.mark.parametrize(("phase", "effect"), PHASE_EFFECTS)
def test_r13_phase_effect_bytes_and_history_survive_position_commit(seeded, phase, effect):
    service, client, project = seeded
    clip = _legacy_clip(service, project)
    clip[phase] = {"type": effect, "duration": 0.25}
    parent_id = _history(service, project)["head"]["revision_id"]
    payload = copy.deepcopy(service.get_project_parent_composition_revision(project, "main", parent_id)["payload"])
    payload["clips"][0] = clip
    _replace_seed_parent(service, project, payload, f"seed-{phase}-{effect}")
    history = _history(service, project)
    parent_id = _history(service, project)["head"]["revision_id"]
    revision = service.get_project_parent_composition_revision(project, "main", parent_id)
    result = _commit_position(service, client, project)
    assert result["data"]["status"] == "accepted"
    saved = result["data"]["canonical_document"]["parent_revision"]["payload"]["clips"][0]
    assert saved[phase] == {"type": effect, "duration": 0.25}
    assert saved["at"] == 1
    assert service.get_project_parent_composition_revision(project, "main", parent_id) == revision
    after = _history(service, project)
    assert len(after["items"]) == len(history["items"]) + 1
    assert after["items"][-1]["records"]["commands"][0]["command"]["op"] == "clip.move"


def _replace_seed_parent(service, project, payload, key):
    # Re-seed a standalone legacy parent while preserving the exact authored clip payload.
    current = _history(service, project)["head"]["revision_id"]
    seed_trusted_parent(service, {
        "project_id": project, "timeline_id": "main", "expected_head": current,
        "parent_revision_id": "r13-parent-" + key,
        "parent_composition": payload, "shot_revisions": [], "internal_timeline_revisions": [],
    }, idempotency_key=key)
