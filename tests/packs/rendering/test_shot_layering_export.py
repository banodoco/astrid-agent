"""Shot row ordering through the managed projection and the real Remotion backend.

This is a stand-in proof, not the owner's film or preview/export pixel parity.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from PIL import Image

from astrid.core import timeline
from astrid.core.rendering.contracts import SCHEMA_VERSION, FrameWindow, RenderRequest, RenderResult
from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition

ROOT = Path(__file__).resolve().parents[3]
REMOTION = ROOT / "remotion"
RUN = ROOT / "astrid/packs/rendering/rendering/renderers/remotion/run.py"
BLUE = (0, 0, 255)
RED = (255, 0, 0)
WIDTH, HEIGHT, FPS = 320, 180, 30


@pytest.fixture
def projected(tmp_path):
    loose = tmp_path / "loose-blue.png"
    shot = tmp_path / "shot-red.png"
    Image.new("RGB", (WIDTH, HEIGHT), BLUE).save(loose)
    # A solid red plate with empty borders: empty shot areas must stay transparent.
    image = Image.new("RGBA", (WIDTH, HEIGHT))
    image.paste((*RED, 255), (80, 45, 240, 135))
    image.save(shot)
    parent = {
        "project_id": "layering-stand-in", "timeline_id": "film", "revision_id": "film-r1",
        "payload": {
            "config": {
                "theme": "banodoco-default",
                "theme_overrides": {"visual": {"canvas": {
                    "width": WIDTH, "height": HEIGHT, "fps": FPS,
                }}},
                "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"},
                           {"id": "type", "kind": "visual", "label": "Type"}],
                "clips": [{"id": "clip-0", "track": "type", "clipType": "image",
                           "asset": "loose", "at": 23.867, "hold": 5.0}],
            },
            "registry": {"assets": {"loose": {
                "media_id": "object-loose", "content_sha256": hashlib.sha256(loose.read_bytes()).hexdigest(),
                "type": "image/png", "resolution": "320x180",
            }}},
            "occurrences": [
                {"occurrence_id": "shot-02", "shot_id": "shot", "shot_revision_id": "shot-r1",
                 "track": "picture", "at_ms": 23867, "duration_ms": 3133},
                {"occurrence_id": "shot-04", "shot_id": "shot", "shot_revision_id": "shot-r2",
                 "track": "picture", "at_ms": 27000, "duration_ms": 3000},
            ],
        },
    }
    shots = [{"shot_id": "shot", "revision_id": "shot-r1",
              "internal_timeline_revision_id": "inside-r1"}]
    internals = [{
        "revision_id": "inside-r1",
        "payload": {
            # Reuse the lower row's source name to catch the old merge-by-id behavior.
            "tracks": [{"id": "type", "kind": "visual", "label": "Shot plate"}],
            "clips": [
                {"id": "red-before-gap", "track": "type", "clip_type": "image",
                 "asset_id": "plate", "at_ms": 0, "duration_ms": 933},
                {"id": "red-after-gap", "track": "type", "clip_type": "image",
                 "asset_id": "plate", "at_ms": 1933, "duration_ms": 1200},
            ],
            "registry": {"assets": {"plate": {
                "media_id": "object-shot", "content_sha256": hashlib.sha256(shot.read_bytes()).hexdigest(),
                "type": "image/png", "resolution": "320x180",
            }}},
        },
    }]
    shots.append({"shot_id": "shot", "revision_id": "shot-r2",
                  "internal_timeline_revision_id": "inside-r2"})
    second = copy.deepcopy(internals[0])
    second["revision_id"] = "inside-r2"
    second["payload"]["clips"] = [{
        "id": "red", "track": "type", "clip_type": "image", "asset_id": "plate",
        "at_ms": 0, "duration_ms": 3000,
    }]
    internals.append(second)
    before = copy.deepcopy((parent, shots, internals))
    projection = project_runtime_parent_composition(
        parent, shot_revisions=shots, internal_timeline_revisions=internals,
    )
    assert (parent, shots, internals) == before
    return projection


def test_export_input_keeps_occurrence_groups_and_empty_shot_windows(projected):
    timeline.validate_timeline(projected.config)
    timeline.validate_registry(projected.registry)
    tracks = projected.config["tracks"]
    assert [track["id"] for track in tracks] == [
        "shot-02~type", "shot-04~type", "picture", "type",
    ]
    assert [track["app"]["group"] for track in tracks[:2]] == [
        {"id": "shot-02", "sourceTrack": "type"},
        {"id": "shot-04", "sourceTrack": "type"},
    ]
    loose, *children = projected.config["clips"]
    assert (loose["at"], loose["hold"], loose["track"]) == (23.867, 5.0, "type")
    assert {clip["track"] for clip in children} == {"shot-02~type", "shot-04~type"}
    assert {clip["asset"] for clip in children} == {"shot-02:plate", "shot-04:plate"}
    assert projected.registry["assets"]["loose"]["content_sha256"] != (
        projected.registry["assets"]["shot-02:plate"]["content_sha256"]
    )
    assert projected.occurrences[0]["at"] < 25 < (
        projected.occurrences[0]["at"] + projected.occurrences[0]["hold"]
    )
    assert not any(clip["at"] <= 25 < clip["at"] + clip["hold"] for clip in children)
    assert len(projected.config["clips"]) == 4  # no opaque filler for empty shot areas


@pytest.mark.integration
def test_real_export_shot_covers_lower_clip_and_empty_areas_reveal_it(
    projected, tmp_path, remotion_runtime_environment,
):
    dependency = json.loads((REMOTION / "package.json").read_text())["dependencies"][
        "@banodoco/timeline-composition"
    ]
    assert dependency.startswith("file:")
    with tarfile.open(REMOTION / dependency.removeprefix("file:")) as archive:
        pinned = json.load(archive.extractfile("package/package.json"))
    installed = json.loads((REMOTION / "node_modules/@banodoco/timeline-composition/package.json").read_text())
    for manifest in (pinned, installed):
        assert (manifest["name"], manifest["version"]) == ("@banodoco/timeline-composition", "0.0.17")

    timeline_path = tmp_path / "projected.timeline.json"
    assets_path = tmp_path / "projected.assets.json"
    # Serialize the entry point's actual output, with no track/clip rewriting.
    timeline_path.write_text(json.dumps(projected.config))
    assets_path.write_text(json.dumps(projected.registry))
    before = (timeline_path.read_bytes(), assets_path.read_bytes())
    request = RenderRequest(
        SCHEMA_VERSION, str(timeline_path), "layering.mp4",
        assets_registry_path=str(assets_path), window=FrameWindow(717, 867, (FPS, 1)),
        materialized_root=str(tmp_path), materialized_objects={
            "object-loose": str(tmp_path / "loose-blue.png"),
            "object-shot": str(tmp_path / "shot-red.png"),
        },
    )
    request_path, result_path = tmp_path / "request.json", tmp_path / "result.json"
    request_path.write_text(json.dumps(request.to_dict()))
    completed = subprocess.run(
        [sys.executable, str(RUN), "render", "--request", str(request_path),
         "--result", str(result_path)],
        cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=480,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(result_path.read_text())
    assert "video" in payload, json.dumps(payload) + completed.stdout + completed.stderr
    result = RenderResult.from_dict(payload)
    assert result.backend_fragments["rendering.remotion"]["renderer"] == "remotion"
    video = tmp_path / result.video.path
    assert video.is_file()
    assert result.video.duration_frames == 150
    assert (timeline_path.read_bytes(), assets_path.read_bytes()) == before

    samples = {}
    # All samples precede the end frame. At 25s shot-02 is active but empty.
    # The corner is also spatially empty throughout both occurrences.
    for frame, expected in ((717, RED), (735, RED), (750, BLUE), (780, RED),
                            (840, RED), (861, RED), (864, RED)):
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video), "-vf",
             f"select=eq(n\\,{frame - request.window.start_frame})", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            check=True, capture_output=True, timeout=30,
        ).stdout
        assert len(decoded) == WIDTH * HEIGHT * 3
        pixels = Image.frombytes("RGB", (WIDTH, HEIGHT), decoded)
        center, corner = pixels.getpixel((160, 90)), pixels.getpixel((20, 20))
        # Codec tolerance for pure colors, not a preview/export comparison.
        assert max(abs(a - b) for a, b in zip(center, expected)) <= 5, (frame, center)
        assert max(abs(a - b) for a, b in zip(corner, BLUE)) <= 5, (frame, corner)
        samples[frame] = {"seconds": frame / FPS, "center": center, "corner": corner}
    print(json.dumps({
        "composition": f"{installed['name']}@{installed['version']}",
        "projected_input_sha256": hashlib.sha256(before[0]).hexdigest(),
        "track_order": [track["id"] for track in projected.config["tracks"]],
        "samples": samples,
    }, sort_keys=True))
