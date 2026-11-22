"""A newly authored built-in transition edit commits and composes in Remotion."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image

from tests.editing_conformance.test_commit_runtime_m1 import open_client
from tests.helpers.trusted_seed import seed_trusted_parent
from tests.integrations.test_managed_media_saved_visualize import _png, _start_host

pytestmark = [pytest.mark.integration, pytest.mark.remotion, pytest.mark.timeout(420)]

TRANSITIONS = ("wipe", "slide-push", "zoom-through")


def _history(service, project):
    return service.list_timeline_commits(project, "main", authorize=lambda: None)


def _patch_transitions(service, project, selections):
    history = _history(service, project)
    head = history["head"]
    changes = [
        {"op": "set", "ref": "parent", "path": ["clips", f"id:{clip_id}", "transition"],
         "before": {"state": "absent"}, "value": {"type": transition, "duration": 0.4}}
        for clip_id, transition in selections
    ]
    body = {
        "api_version": 1, "project_id": project, "timeline_id": "main",
        "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
        "commands": [{"schema_version": 1, "op": "doc.patch", "version": "1",
                       "select": None, "args": {"changes": changes}}],
        "label": "Choose built-in transitions", "merge": "fields", "reads": [],
        "steps": [{"id": "edit", "label": "Choose built-in transitions",
                   "intent": "Choose built-in transitions", "tags": [],
                   "command_ordinals": [0], "patch": changes}],
    }
    return service.commit_timeline(
        project, "main", body, idempotency_key="r14-built-in-transitions",
        publisher="owner", invocation_path=f"/v1/projects/{project}/timelines/main/commit",
        authorize=lambda: None,
    )


def test_new_builtin_transition_choices_commit_and_visualize(real_runtime, tmp_path):
    daemon = real_runtime(authoring_profile="astrid", support_root=tmp_path / "runtime")
    service = daemon.service
    project = service.create_project({"slug": "r14-transitions", "name": "R14 transitions"})["id"]
    service.create_timeline(project, "main", idempotency_key="birth")

    # These are the authoring choices exposed by the installed Agent catalog.
    from astrid.core.element import catalog

    exposed = set(catalog.list_transition_ids())
    assert set(TRANSITIONS) <= exposed, exposed

    assets = {}
    clips = []
    colors = ("red", "blue", "lime", "yellow", "magenta", "cyan")
    for index, color in enumerate(colors):
        path = _png(tmp_path / f"source-{index}.png", color)
        media_id = service.ingest(
            project, path.read_bytes(), original_name=path.name, media_type="image/png",
            idempotency_key=f"source-{index}",
        )["data"]["object_id"]
        asset_id = f"asset-{index}"
        assets[asset_id] = {
            "media_id": media_id, "content_sha256": media_id.removeprefix("sha256:"),
            "type": "image", "resolution": "320x180",
        }
        clips.append({"id": f"clip-{index}", "track": "V1", "clipType": "image",
                      "asset": asset_id, "at": index * 0.8, "hold": 0.8})

    seed_trusted_parent(service, {
        "project_id": project, "timeline_id": "main", "parent_revision_id": "r14-six-clip-parent",
        "parent_composition": {
            "config": {"output": {"file": "out.mp4", "fps": 30, "resolution": "320x180"},
                       "tracks": [{"id": "V1", "kind": "visual", "label": "Picture"}]},
            "registry": {"assets": assets}, "clips": clips, "occurrences": [],
        },
        "shot_revisions": [], "internal_timeline_revisions": [],
    }, idempotency_key="seed-r14-six-clip-parent")

    client = open_client(daemon)
    before = _history(service, project)
    selections = [("clip-1", "wipe"), ("clip-3", "slide-push"), ("clip-5", "zoom-through")]
    commit = _patch_transitions(service, project, selections)
    assert commit["data"]["status"] == "accepted", commit
    saved_clips = commit["data"]["canonical_document"]["parent_revision"]["payload"]["clips"]
    saved = {clip["id"]: clip["transition"] for clip in saved_clips if "transition" in clip}
    expected = {clip_id: {"type": identity, "duration": 0.4} for clip_id, identity in selections}
    assert {clip_id: saved[clip_id] for clip_id, _ in selections} == expected
    after = _history(service, project)
    assert len(after["items"]) == len(before["items"]) + 1
    assert after["items"][-1]["records"]["commands"][0]["command"]["op"] == "doc.patch"

    host = None
    env_keys = ("ASTRID_RUNTIME_ENDPOINT", "ASTRID_RUNTIME_CREDENTIAL", "ASTRID_RUNTIME_DATA_ROOT")
    prior = {key: os.environ.get(key) for key in env_keys}
    try:
        host = _start_host(daemon)
        assert "rendering.timeline_visualize" in host["host_ready_capabilities"]
        result = client.timelines.visualize(
            project, "main", mode="composed", formats=("png", "md"),
            options={"range": "0.6..3.9", "every": 0.2, "resolution": "320x180"},
        )
        assert result.ok, json.dumps(result.to_dict())
        frame_index_path = Path(result.outputs["frame_index"])
        index = json.loads(frame_index_path.read_text())
        assert index["schema"] == "astrid.filmstrip.v2", index
        cards = {card["frame"]: card for card in index["cards"]}
        # Each midpoint is 0.2 seconds before the incoming clip's authored start.
        assert {18, 66, 114} <= cards.keys(), cards.keys()
        capture = index["provenance"]["frame_capture"]
        assert capture["fresh_frames"] + capture["cached_frames"] > 0, capture
        expected_pixels = {
            18: ((0, 0, 255), (255, 0, 0)),      # Wipe reveals blue from the left.
            66: ((0, 255, 0), (255, 255, 0)),    # Slide brings yellow in from the right.
            114: ((128, 128, 255), (128, 128, 255)),  # Zoom blends magenta and cyan.
        }
        for frame, expected_pair in expected_pixels.items():
            with Image.open(frame_index_path.parent / cards[frame]["image"]) as image:
                rgb = image.convert("RGB")
                for x, expected_pixel in zip((60, 230), expected_pair):
                    pixel = rgb.getpixel((x, 90))
                    assert max(abs(actual - target) for actual, target in zip(pixel, expected_pixel)) <= 16, (
                        frame, x, pixel, expected_pixel,
                    )
        assert _history(service, project) == after
    finally:
        try:
            if host:
                from astrid.sdk.upgrade import stop_pack_host

                stop_pack_host(tmp_path)
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
