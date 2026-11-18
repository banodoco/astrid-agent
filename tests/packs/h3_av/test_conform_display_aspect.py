"""Source conforming honors display aspect ratio when source pixels are anamorphic."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.generation.engine_facts import comfyui_facts
from astrid.packs.h3_av.shared import engine_facts
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request


def _anamorphic_source(path: Path) -> None:
    # A 640x480 raster with SAR 3:2 has 2:1 display aspect. Red side strips sit
    # outside the 16:9 cover crop but remain visible when the full image is fit.
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "color=c=green:s=640x480:r=24:d=1.5",
         "-vf", "drawbox=x=0:y=0:w=20:h=480:color=red:t=fill,"
         "drawbox=x=620:y=0:w=20:h=480:color=red:t=fill,setsar=3/2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "1.5", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )


def _square_source(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "color=c=green:s=640x480:r=24:d=1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-t", "1.5", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )


def _pixel(rgb: bytes, x: int, y: int) -> tuple[int, int, int]:
    index = (y * 1024 + x) * 3
    return tuple(rgb[index:index + 3])


def _frame(path: Path) -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True, timeout=30,
    )
    assert len(result.stdout) == 1024 * 576 * 3
    return result.stdout


@pytest.mark.parametrize("fit", ["fill", "fit"])
def test_conform_preserves_anamorphic_display_aspect_for_fill_and_fit(tmp_path: Path, monkeypatch, fit: str) -> None:
    _anamorphic_source(tmp_path / "source.mp4")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,sample_aspect_ratio,display_aspect_ratio", "-of", "json",
         str(tmp_path / "source.mp4")],
        check=True, capture_output=True, text=True, timeout=30,
    )
    source_info = json.loads(probe.stdout)["streams"][0]
    assert (source_info["width"], source_info["height"]) == (640, 480)
    assert source_info["sample_aspect_ratio"] == "3:2"
    assert source_info["display_aspect_ratio"] == "2:1"

    monkeypatch.setattr(engine_facts, "_FACTS", comfyui_facts())
    request = normalize_request({
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source", "range": [0, 1], "fit": fit},
        "output": {"duration": 1},
        "content": {"prompt": "Preserve the source geometry."},
        "changes": {"video": [{"during": [0, 1], "area": {"full_frame": True},
                                  "action": "generate"}], "audio": []},
        "references": [],
        "overrides": {},
    })
    preparation = prepare_request(request, asset_map={"source": str(tmp_path / "source.mp4")},
                                  out_dir=tmp_path / f"prepared-{fit}")
    conform = preparation["source_conform"]
    output = tmp_path / f"prepared-{fit}" / "conformed-source" / conform["output"]["file"]
    assert conform["output"]["fit"] == fit
    assert conform["output"]["width"] == 1024
    assert conform["output"]["height"] == 576
    assert output.is_file()

    rgb = _frame(output)
    left_center = _pixel(rgb, 4, 288)
    top_center = _pixel(rgb, 512, 4)
    if fit == "fit":
        assert left_center[0] > left_center[1] * 1.5  # retained red side strip
        assert max(top_center) < 32  # black letterbox from 2:1 into 16:9
    else:
        assert left_center[1] > left_center[0] * 1.5  # side strip cropped by cover
        assert top_center[1] > top_center[0] * 1.5  # picture fills the top edge


def test_conform_records_square_pixel_assumption_when_sar_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "square-with-missing-sar.mp4"
    _square_source(source)
    from astrid.packs.h3_av.shared import conform

    real_probe = conform._probe

    def probe_without_source_sar(path: Path) -> dict:
        info = real_probe(path)
        if Path(path).resolve() == source.resolve():
            next(row for row in info["streams"] if row.get("codec_type") == "video").pop(
                "sample_aspect_ratio", None)
        return info

    monkeypatch.setattr(conform, "_probe", probe_without_source_sar)
    monkeypatch.setattr(engine_facts, "_FACTS", comfyui_facts())
    request = normalize_request({
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source", "range": [0, 1], "fit": "fit"},
        "output": {"duration": 1},
        "content": {"prompt": "Preserve the source geometry."},
        "changes": {"video": [{"during": [0, 1], "area": {"full_frame": True},
                                  "action": "generate"}], "audio": []},
        "references": [],
        "overrides": {},
    })
    preparation = prepare_request(request, asset_map={"source": str(source)},
                                  out_dir=tmp_path / "prepared-unknown-sar")
    conform_record = preparation["source_conform"]
    assert conform_record["source"]["sample_aspect_ratio"] == "1:1"
    assert conform_record["source"]["sample_aspect_assumed_square"] is True
    output = tmp_path / "prepared-unknown-sar" / "conformed-source" / conform_record["output"]["file"]
    rgb = _frame(output)
    assert max(_pixel(rgb, 4, 288)) < 32  # assumed 4:3 picture is fit with pillar bars
