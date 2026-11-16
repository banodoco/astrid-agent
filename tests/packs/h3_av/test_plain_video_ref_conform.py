"""Plain video_ref inputs select target-frame boundaries before source conforming."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from fractions import Fraction
from pathlib import Path

from astrid.core.generation.engine_facts import comfyui_facts
from astrid.packs.h3_av.shared import engine_facts
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request


def _video(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=24000/1001:duration=3.25",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3.25",
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )


def _load_hooks():
    path = (Path(__file__).resolve().parents[3] /
            "astrid/packs/h3_av/extensions/vibecomfy.workflows/hooks.py")
    spec = importlib.util.spec_from_file_location("h3_plain_video_ref_hooks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plain_video_ref_selects_and_conforms_whole_target_frames(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source-23976.mp4"
    _video(source)
    facts = comfyui_facts()
    monkeypatch.setattr(engine_facts, "_FACTS", facts)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,avg_frame_rate", "-of", "json", str(source)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    video_stream = json.loads(probe.stdout)["streams"][0]
    assert video_stream["r_frame_rate"] == video_stream["avg_frame_rate"] == "24000/1001"

    document, assets = _load_hooks()._plain_request(
        {"prompt": "Preserve the audiovisual scene."}, {"video_ref": str(source)}, facts,
    )
    request = normalize_request(document)
    start, end = request.value["source"]["range"]
    selected_frames = Fraction(str(end)) * 24
    assert start == 0
    assert selected_frames.denominator == 1
    assert selected_frames == 78
    assert request.value["output"]["duration"] == 78 / 24
    assert assets == {"video_ref": str(source)}

    preparation = prepare_request(request, asset_map=assets, out_dir=tmp_path / "prepared")
    conform = preparation["source_conform"]
    assert conform["output"]["fps"] == 24
    assert conform["output"]["frames"] == 78
    assert conform["output"]["range_seconds"] == [0, 3.25]
    assert conform["audio"]["sample_rate"] == 48000
    assert conform["decoded_av"]["has_audio"] is True
    conformed = tmp_path / "prepared" / "conformed-source" / conform["output"]["file"]
    assert conformed.is_file()
