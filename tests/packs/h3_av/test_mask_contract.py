from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.errors import PreparationError
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.shared.request_v2 import H3RequestError

W, H, FRAMES = 64, 48, 24


def _frame(value: int, *, on: bool = True) -> bytes:
    rows = bytearray(W * H)
    if on:
        for y in range(10, 22):
            for x in range(10, 22):
                rows[y * W + x] = value
    return bytes(rows)


def _mask_video(path: Path, *, size=(W, H), frames=FRAMES, value=255, on=range(FRAMES), extra=None) -> None:
    width, height = size
    chunks = []
    for i in range(frames):
        if size != (W, H):
            chunks.append(bytes(width * height))
        elif extra and i in extra:
            chunks.append(_frame(extra[i]))
        else:
            chunks.append(_frame(value, on=i in on))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{width}x{height}", "-r", "24", "-i", "-", "-c:v", "ffv1", "-pix_fmt", "gray", str(path)],
        input=b"".join(chunks), check=True, capture_output=True,
    )


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size={W}x{H}:rate=24", "-frames:v", str(FRAMES), "-c:v", "ffv1", str(source)],
        check=True, capture_output=True,
    )
    return source


def _prepare(tmp_path: Path, mask: Path, *, during=(0, 1), mask_spec=None):
    source = _source(tmp_path)
    request = normalize_request({
        "version": 2,
        "prompt": "Edit inside the moving mask.",
        "duration": 1,
        "media": [{
            "id": "src", "asset": "source.mkv", "role": "timeline", "modality": "video",
            "at": {"frame": 0},
            "edit": [{"stream": "video", "during": list(during),
                      "mask": mask_spec if mask_spec is not None else {"asset": "mouth-mask"}}],
        }],
    })
    return prepare_request(request, asset_map={"source.mkv": str(source), "mouth-mask": str(mask)})


@pytest.mark.ffmpeg
def test_valid_mask_video_accepted_and_edge_carried(tmp_path: Path) -> None:
    mask = tmp_path / "m.mkv"
    _mask_video(mask)
    spec = {"asset": {"id": "mouth-mask", "modality": "video"}, "edge": {"dilate": 3, "feather": 2}, "track": {"x": 1}}
    prep = _prepare(tmp_path, mask, mask_spec=spec)
    area = prep["mask_schedule"]["video"]["changes"][0]["area"]
    assert area["asset"] == "mouth-mask"
    assert area["edge"] == {"dilate": 3, "feather": 2}
    assert area["track"] == {"x": 1}


@pytest.mark.ffmpeg
def test_edge_defaults_to_zero(tmp_path: Path) -> None:
    mask = tmp_path / "m.mkv"
    _mask_video(mask)
    area = _prepare(tmp_path, mask)["mask_schedule"]["video"]["changes"][0]["area"]
    assert area["edge"] == {"dilate": 0, "feather": 0}


@pytest.mark.ffmpeg
def test_threshold_binarizes_for_the_check(tmp_path: Path) -> None:
    mask = tmp_path / "m.mkv"
    _mask_video(mask, value=128)
    with pytest.raises(PreparationError, match="mouth-mask"):
        _prepare(tmp_path, mask)
    _prepare(tmp_path, mask, mask_spec={"asset": "mouth-mask", "threshold": 100})


@pytest.mark.ffmpeg
@pytest.mark.parametrize("variant", ["wrong_size", "wrong_count", "values_128", "outside_during"])
def test_invalid_mask_video_rejected(tmp_path: Path, variant: str) -> None:
    mask = tmp_path / "m.mkv"
    during = (0, 1)
    if variant == "wrong_size":
        _mask_video(mask, size=(32, 24))
    elif variant == "wrong_count":
        _mask_video(mask, frames=23)
    elif variant == "values_128":
        _mask_video(mask, extra={5: 128, 6: 0})
    else:
        during = (0.5, 1)
        _mask_video(mask)  # on for every frame, including those before 0.5s
    with pytest.raises(PreparationError, match="mouth-mask"):
        _prepare(tmp_path, mask, during=during)


@pytest.mark.ffmpeg
def test_edge_with_full_frame_rejected(tmp_path: Path) -> None:
    mask = tmp_path / "m.mkv"
    _mask_video(mask)
    with pytest.raises(H3RequestError, match="edge"):
        _prepare(tmp_path, mask, mask_spec={"full_frame": True, "edge": {"dilate": 1}})
