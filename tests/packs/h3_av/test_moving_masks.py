from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.actions.verify.verify import VerificationError, verify_candidate
from astrid.packs.h3_av.shared.compose import CompositionError, compose_candidate
from astrid.packs.h3_av.shared.request import normalize_request

W, H, SQ, FRAMES = 64, 48, 12, 24


def _square_frame(index: int) -> bytes:
    x0, y0 = 2 * index, 10
    rows = bytearray(W * H)
    for y in range(y0, y0 + SQ):
        for x in range(x0, x0 + SQ):
            rows[y * W + x] = 255
    return bytes(rows)


def _mask_video(path: Path, *, codec: str = "ffv1", size=(W, H), frames=FRAMES) -> None:
    width, height = size
    raw = b"".join(
        (_square_frame(i) if size == (W, H) else bytes(width * height)) for i in range(frames)
    )
    codec_args = ["-c:v", "ffv1", "-pix_fmt", "gray"] if codec == "ffv1" else [
        "-c:v", "libx264", "-pix_fmt", "yuv420p"
    ]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}", "-r", "24", "-i", "-",
         *codec_args, str(path)],
        input=raw, check=True, capture_output=True,
    )


def _fixtures(tmp_path: Path, mask_name: str = "mask.mkv"):
    source = tmp_path / "source.mkv"
    generated = tmp_path / "generated.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-frames:v", "24", "-t", "1", "-c:v", "ffv1", "-pix_fmt", "yuv420p",
         "-c:a", "pcm_s16le", str(source)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=64x48:r=24",
         "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
         "-frames:v", "24", "-t", "1", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(generated)],
        check=True, capture_output=True,
    )
    return source, generated


def _prepare(source: Path, mask: Path):
    request = normalize_request({
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source.mkv", "range": [0, 1]},
        "output": {"duration": 1},
        "content": {"prompt": "Edit only inside the moving square."},
        "changes": {
            "video": [{
                "during": [0, 1],
                "area": {"rectangle": [0, 0, 64, 48]},
                "mask_asset": mask.name,
                "action": "generate",
            }],
            "audio": [],
        },
        "references": [],
        "overrides": {},
    })
    return prepare_request(request, asset_map={"source.mkv": str(source), mask.name: str(mask)})


def _composed(tmp_path: Path):
    source, generated = _fixtures(tmp_path)
    mask = tmp_path / "mask.mkv"
    _mask_video(mask)
    preparation = _prepare(source, mask)
    composition = compose_candidate(
        preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "composition"
    )
    return source, mask, preparation, composition


@pytest.mark.ffmpeg
def test_mask_video_composes_and_verifies_per_frame(tmp_path: Path) -> None:
    source, _mask, preparation, composition = _composed(tmp_path)
    segment = composition["composition"]["video_segments"][0]
    assert segment["mask_kind"] == "video"
    assert segment["mask_frames"] == FRAMES
    verification = verify_candidate(preparation=preparation, composition=composition, source=source)
    evidence = verification["preservation"]["spatial_complement"][0]
    assert evidence["mask_kind"] == "video"
    assert evidence["protected_pixel_count_per_frame"] == [W * H - SQ * SQ] * FRAMES
    assert evidence["max_abs_channel_difference"] == 0


@pytest.mark.ffmpeg
def test_mask_video_tamper_inside_union_outside_frame_fails(tmp_path: Path) -> None:
    source, _mask, preparation, composition = _composed(tmp_path)
    # Frame 13's square spans x 26..37; x 0..1 is in frame 0's square only.
    tampered = tmp_path / "tampered.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", composition["candidate"]["path"], "-map", "0:v:0", "-map", "0:a:0",
         "-vf", "drawbox=x=0:y=10:w=2:h=2:color=white:t=fill:enable='eq(n,13)'",
         "-c:v", "ffv1", "-pix_fmt", "gbrp", "-c:a", "pcm_s16le", str(tampered)],
        check=True, capture_output=True,
    )
    composition["candidate"].update({
        "path": str(tampered),
        "sha256": hashlib.sha256(tampered.read_bytes()).hexdigest(),
        "size": tampered.stat().st_size,
    })
    with pytest.raises(VerificationError, match="outside the declared regional video mask"):
        verify_candidate(preparation=preparation, composition=composition, source=source)


@pytest.mark.ffmpeg
@pytest.mark.parametrize("variant", ["lossy", "wrong_size", "wrong_count"])
def test_mask_video_rejects_lossy_wrong_size_wrong_count(tmp_path: Path, variant: str) -> None:
    source, generated = _fixtures(tmp_path)
    mask = tmp_path / f"{variant}.mkv"
    if variant == "lossy":
        _mask_video(mask, codec="h264")
    elif variant == "wrong_size":
        _mask_video(mask, size=(32, 24))
    else:
        _mask_video(mask, frames=23)
    with pytest.raises(CompositionError):
        preparation = _prepare(source, mask)
        compose_candidate(
            preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "composition"
        )


@pytest.mark.ffmpeg
def test_swapped_mask_file_fails_provenance(tmp_path: Path) -> None:
    source, mask, preparation, composition = _composed(tmp_path)
    # Same path and asset id, different (still valid) mask video.
    other = tmp_path / "other.mkv"
    raw = b"".join(bytes(W * H) for _ in range(FRAMES))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r", "24", "-i", "-",
         "-c:v", "ffv1", "-pix_fmt", "gray", str(other)],
        input=raw, check=True, capture_output=True,
    )
    mask.write_bytes(other.read_bytes())
    with pytest.raises(VerificationError, match="mask provenance"):
        verify_candidate(preparation=preparation, composition=composition, source=source)
