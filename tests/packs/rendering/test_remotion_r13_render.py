"""Pixel proof for R13 transitions and animation phases on the real renderer."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from astrid.core.rendering.contracts import SCHEMA_VERSION, RenderRequest
from astrid.core import timeline

ROOT = Path(__file__).resolve().parents[3]
RUN = ROOT / "astrid/packs/rendering/rendering/renderers/remotion/run.py"
WIDTH, HEIGHT, FPS = 320, 180, 30


def _solid(path: Path, rgb: tuple[int, int, int], *, mark: bool = False) -> str:
    image = Image.new("RGB", (WIDTH, HEIGHT), rgb)
    if mark:
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 60, 75, 120), fill=(255, 255, 255))
        draw.rectangle((235, 35, 290, 145), fill=(255, 220, 0))
    image.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decode(video: Path, frame: int) -> Image.Image:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vf",
         f"select=eq(n\\,{frame})", "-frames:v", "1", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"], check=True, capture_output=True, timeout=30,
    ).stdout
    assert len(raw) == WIDTH * HEIGHT * 3, (frame, len(raw))
    return Image.frombytes("RGB", (WIDTH, HEIGHT), raw)


def _near(actual: tuple[int, ...], expected: tuple[int, ...], tolerance: int = 8) -> bool:
    return max(abs(a - b) for a, b in zip(actual, expected)) <= tolerance


@pytest.mark.remotion
@pytest.mark.timeout(600)
def test_r13_transitions_and_legacy_phases_change_real_remotion_pixels(
    tmp_path: Path, remotion_runtime_environment: None,
) -> None:
    """Render one compact film, then inspect overlap boundaries and phase pixels."""
    colors = [(24, 48, 220), (22, 180, 54), (220, 38, 34), (232, 190, 25)]
    paths = [tmp_path / f"plate-{index}.png" for index in range(5)]
    digests = [_solid(paths[i], colors[i]) for i in range(4)]
    digests.append(_solid(paths[4], (30, 150, 200), mark=True))
    tracks = [{"id": "visual", "kind": "visual", "label": "Visual"}]
    clips: list[dict[str, object]] = []
    transitions = ("wipe", "slide-push", "zoom-through", "crossfade")
    period_frames = 84
    for index, identity in enumerate(transitions):
        base = index * period_frames
        clips.append({"id": f"source-{identity}", "track": "visual", "clipType": "image",
                      "asset": f"plate-{index}", "at": base / FPS, "hold": 2.4})
        clips.append({"id": f"incoming-{identity}", "track": "visual", "clipType": "image",
                      "asset": f"plate-{(index + 1) % 4}", "at": (base + 60) / FPS,
                      "hold": 0.8, "transition": {"type": identity, "duration": 0.4}})

    # Entrance, exit and continuous phases each get a patterned foreground over
    # a contrasting plate so a translated/scaled image changes sampled pixels.
    phase_specs = (
        ("entrance", "slide-left", 336, {"type": "slide-left", "duration": 0.4}),
        ("exit", "fade-out", 420, {"type": "fade-out", "duration": 0.4}),
        ("continuous", "ken-burns", 504, {"type": "ken-burns", "intensity": 1.0}),
    )
    for slot, identity, start, reference in phase_specs:
        clips.append({"id": f"phase-bg-{slot}", "track": "visual", "clipType": "image",
                      "asset": "plate-3", "at": start / FPS, "hold": 1.2})
        clips.append({"id": f"phase-{slot}", "track": "visual", "clipType": "image",
                      "asset": "plate-4", "at": start / FPS, "hold": 1.2, slot: reference})

    config = {
        "theme": "banodoco-default",
        "theme_overrides": {"visual": {"canvas": {"width": WIDTH, "height": HEIGHT, "fps": FPS}}},
        "tracks": tracks,
        "clips": clips,
    }
    timeline_path, assets_path = tmp_path / "r13.timeline.json", tmp_path / "r13.assets.json"
    timeline.save_timeline(config, timeline_path)
    asset_records = {
        f"plate-{i}": {"media_id": f"object-{i}", "content_sha256": digests[i],
                       "type": "image/png", "resolution": f"{WIDTH}x{HEIGHT}"}
        for i in range(5)
    }
    timeline.save_registry({"assets": asset_records}, assets_path)
    output = tmp_path / "r13-render.mp4"
    request = RenderRequest(
        schema_version=SCHEMA_VERSION,
        timeline_path=str(timeline_path),
        output_name=output.name,
        assets_registry_path=str(assets_path),
        materialized_root=str(tmp_path),
        materialized_objects={f"object-{i}": str(paths[i]) for i in range(5)},
    )
    request_path, result_path = tmp_path / "request.json", tmp_path / "result.json"
    request_path.write_text(json.dumps(request.to_dict()), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(RUN), "render", "--request", str(request_path),
         "--result", str(result_path)], cwd=ROOT, env=dict(os.environ),
        capture_output=True, text=True, timeout=540,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert "video" in payload, json.dumps(payload) + completed.stdout + completed.stderr
    video = tmp_path / payload["video"]["path"]
    assert video.is_file()

    observed: dict[str, dict[str, object]] = {}
    for index, identity in enumerate(transitions):
        base = index * period_frames
        # Transition starts at authored-at minus 12 frames: frame 47 is outside,
        # 48 is p=0, 54 is p=.5, 59 is the last interior sample, 60 is p=1.
        frames = {"before": base + 47, "first": base + 48, "middle": base + 54,
                  "last": base + 59, "after": base + 60, "settled": base + 61}
        pixels = {name: _decode(video, number) for name, number in frames.items()}
        center = pixels["middle"].getpixel((160, 90))
        left = pixels["middle"].getpixel((35, 90))
        right = pixels["middle"].getpixel((285, 90))
        assert _near(pixels["before"].getpixel((160, 90)), colors[index]), (identity, "before")
        assert _near(pixels["after"].getpixel((160, 90)), colors[(index + 1) % 4]), (identity, "after")
        assert _near(pixels["settled"].getpixel((160, 90)), colors[(index + 1) % 4]), (identity, "settled")
        if identity == "wipe":
            assert _near(left, colors[(index + 1) % 4]) and _near(right, colors[index]), (identity, left, right)
        elif identity == "slide-push":
            assert _near(left, colors[index]) and _near(right, colors[(index + 1) % 4]), (identity, left, right)
        elif identity == "zoom-through":
            assert not _near(center, colors[index]) and not _near(center, colors[(index + 1) % 4]), (identity, center)
        else:
            assert all(abs(a - b) > 12 for a, b in zip(center, colors[index])), (identity, center)
        observed[identity] = {"frames": frames, "middle_left": left, "middle_center": center,
                              "middle_right": right}

    # Representative actual pixel behavior for each legacy phase slot.
    phase_result: dict[str, object] = {}
    for slot, identity, start, _reference in phase_specs:
        if slot == "entrance":
            sample_frames = (start, start + 6, start + 11)
            point = (45, 90)
        elif slot == "exit":
            sample_frames = (start + 24, start + 30, start + 35)
            point = (45, 90)
        else:
            sample_frames = (start, start + 18)
            point = (75, 90)
        samples = [_decode(video, frame).getpixel(point) for frame in sample_frames]
        assert any(not _near(pixel, (30, 150, 200)) for pixel in samples), (slot, identity, samples)
        assert len({tuple(pixel) for pixel in samples}) > 1, (slot, identity, samples)
        phase_result[slot] = {"id": identity, "frames": sample_frames, "pixels": samples}

    # Unknown legacy IDs remain refused at the renderer boundary.
    from astrid.packs.rendering.rendering.renderers.remotion import run as remotion
    with pytest.raises(ValueError, match="unregistered transition 'unknown-r13-transition'"):
        remotion._resolve_timeline_element_references(
            {"clips": [{"id": "unknown", "transition": {"type": "unknown-r13-transition"}}]},
            theme_path=None, registry=remotion._load_render_element_registry(),
        )
    print(json.dumps({"renderer": "rendering.remotion", "transitions": observed,
                      "phases": phase_result}, sort_keys=True))
