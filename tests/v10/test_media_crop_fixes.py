"""Review-fix coverage for ``media crop``: video rows, re-runs, bars, images, audio, CLI."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageCms, ImageOps

from astrid.core.cli.domain_product import run_product_family
from astrid.core.media import ffprobe_metadata_strict
from astrid.core.media_crop import Box, crop_image_bytes, crop_media, detect_bars, image_size
from astrid.sdk.contracts import DomainResult, ErrorObject

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")


# -- fakes ------------------------------------------------------------------


class _Runtime:
    """Mimics the runtime: content-addressed objects, key replay, unique relations."""

    def __init__(self, rows: dict[str, tuple[dict[str, Any], bytes]]) -> None:
        self.rows = rows
        self.objects: dict[str, bytes] = {}
        self.import_keys: list[str] = []
        self.relate_keys: list[str] = []
        self.relations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.replays: dict[str, dict[str, Any]] = {}
        self.relate_failure: ErrorObject | None = None

    def show(self, project, ref):
        if ref not in self.rows:
            return DomainResult.failure(ErrorObject("not_found", "missing", {}))
        return DomainResult.success(dict(self.rows[ref][0]))

    def read_bytes(self, object_id):
        for row, data in self.rows.values():
            if row["object_id"] == object_id:
                return data
        raise AssertionError(object_id)

    def import_file(self, *, project, path, realm="managed_local", idempotency_key=None):
        self.import_keys.append(idempotency_key)
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        object_id = f"sha256:{digest}"
        self.objects[object_id] = data
        return DomainResult.success(
            {"object_id": object_id, "digest": digest, "filename": Path(path).name},
            idempotency_key=idempotency_key or "",
        )

    def relate(self, project, *, from_object_id, to_object_id, kind, metadata=None, idempotency_key=None):
        self.relate_keys.append(idempotency_key)
        if self.relate_failure is not None:
            return DomainResult.failure(self.relate_failure, idempotency_key=idempotency_key or "")
        if idempotency_key in self.replays:
            return DomainResult.success(dict(self.replays[idempotency_key]), idempotency_key=idempotency_key)
        triple = (from_object_id, to_object_id, kind)
        if triple in self.relations:
            return DomainResult.failure(
                ErrorObject("conflict", "media relation already exists", {}),
                idempotency_key=idempotency_key or "",
            )
        self.relations[triple] = dict(metadata or {})
        result = {"from_object_id": from_object_id, "to_object_id": to_object_id, "kind": kind}
        self.replays[idempotency_key] = result
        return DomainResult.success(dict(result), idempotency_key=idempotency_key or "")

    def list_relations(self, project, *, cursor=None, limit=50):
        items = [
            {"from_object_id": f, "to_object_id": t, "kind": k, "metadata": m}
            for (f, t, k), m in self.relations.items()
        ]
        return DomainResult.success({"items": items, "next_cursor": None})


class _Client:
    def __init__(self, rows: dict[str, tuple[dict[str, Any], bytes]]) -> None:
        self.media = _Runtime(rows)


def _row(object_id: str, data: bytes, name: str, media_type: str) -> tuple[dict[str, Any], bytes]:
    return (
        {"object_id": object_id, "digest": hashlib.sha256(data).hexdigest(), "filename": name, "media_type": media_type},
        data,
    )


def _png_bytes(arr: np.ndarray, **options: Any) -> bytes:
    out = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8), "RGB").save(out, format="PNG", **options)
    return out.getvalue()


def _bars_png(width: int = 320, height: int = 200, top: int = 20, bottom: int = 30, seed: int = 3) -> bytes:
    rng = np.random.default_rng(seed)
    arr = rng.integers(60, 256, size=(height, width, 3), dtype=np.uint8)
    arr[:top] = 0
    arr[height - bottom:] = 0
    return _png_bytes(arr)


def _imported(client: _Client, index: int = 0) -> bytes:
    return list(client.media.objects.values())[index]


def _ffprobe_audio_codec(path: Path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


# -- video: odd y offset ----------------------------------------------------


def _ramp_video(path: Path, *, width: int = 160, height: int = 240, black_rows: int = 121, frames: int = 5) -> None:
    """Top ``black_rows`` are pure black; picture row r has value 20 + (r - black_rows)."""
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    for row in range(black_rows, height):
        arr[row] = 20 + (row - black_rows)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", "25", "-i", "-",
            "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        input=arr.tobytes() * frames, check=True, capture_output=True,
    )


def _first_frame_rgb(path: Path, width: int, height: int) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)


@needs_ffmpeg
def test_video_crop_odd_y_starts_on_the_requested_row(tmp_path: Path) -> None:
    src = tmp_path / "ramp.mp4"
    _ramp_video(src)
    client = _Client({"M-vid": _row("O-vid", src.read_bytes(), "ramp.mp4", "video/mp4")})

    result = crop_media(client, project="P", ref="M-vid", box="0,121,160,118")

    assert result.ok, result.error
    assert result.data["box"] == {"x": 0, "y": 121, "w": 160, "h": 118}
    out = tmp_path / "out.mp4"
    out.write_bytes(_imported(client))
    probe = ffprobe_metadata_strict(out)
    assert (probe.width, probe.height) == (160, 118)
    frame = _first_frame_rgb(out, 160, 118)
    # Row 0 must be picture (source row 121, value 20), not the black bar above it.
    for row in (0, 1, 60, 117):
        assert abs(float(frame[row].mean()) - (20 + row)) < 8, row
    recorded = next(iter(client.media.relations.values()))
    assert recorded["box"] == result.data["box"]


# -- video: bar sampling ----------------------------------------------------


@needs_ffmpeg
def test_video_bars_ignore_a_black_frame_at_the_midpoint(tmp_path: Path) -> None:
    src = tmp_path / "cards.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=white:s=160x120:r=25:d=1",
            "-f", "lavfi", "-i", "color=c=black:s=160x160:r=25:d=1",
            "-f", "lavfi", "-i", "color=c=white:s=160x120:r=25:d=1",
            "-filter_complex",
            "[0:v]pad=160:160:0:20:black[a];[1:v]null[b];[2:v]pad=160:160:0:20:black[c];"
            "[a][b][c]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src),
        ],
        check=True, capture_output=True,
    )
    client = _Client({"M-vid": _row("O-vid", src.read_bytes(), "cards.mp4", "video/mp4")})

    result = crop_media(client, project="P", ref="M-vid", remove_bars=True, dry_run=True)

    assert result.ok, result.error
    assert result.data["box"] == {"x": 0, "y": 20, "w": 160, "h": 120}
    assert result.data["detected"]["edges"] == {"top": 20, "bottom": 20, "left": 0, "right": 0}


@needs_ffmpeg
def test_video_audio_reencoded_when_copy_cannot_go_into_mp4(tmp_path: Path) -> None:
    src = tmp_path / "clip.mkv"
    made = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=white:s=160x120:r=25:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "libvorbis", "-shortest", str(src),
        ],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("ffmpeg has no libvorbis encoder")
    client = _Client({"M-mkv": _row("O-mkv", src.read_bytes(), "clip.mkv", "video/x-matroska")})

    result = crop_media(client, project="P", ref="M-mkv", box="0,0,160,120")

    assert result.ok, result.error
    assert result.data["audio"] == {"action": "reencode", "codec": "vorbis", "output_codec": "aac"}
    out = tmp_path / "out.mp4"
    out.write_bytes(_imported(client))
    assert _ffprobe_audio_codec(out) == "aac"


@needs_ffmpeg
def test_video_mp4_aac_audio_is_copied(tmp_path: Path) -> None:
    src = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=white:s=160x120:r=25:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
        ],
        check=True, capture_output=True,
    )
    client = _Client({"M-mp4": _row("O-mp4", src.read_bytes(), "clip.mp4", "video/mp4")})

    result = crop_media(client, project="P", ref="M-mp4", box="0,0,160,120")

    assert result.ok, result.error
    assert result.data["audio"] == {"action": "copy", "codec": "aac"}


# -- bar detection ----------------------------------------------------------


def test_dark_but_textured_edge_is_not_removed() -> None:
    rng = np.random.default_rng(11)
    arr = rng.integers(60, 256, size=(200, 160, 3), dtype=np.uint8)
    arr[:40] = rng.integers(0, 13, size=(40, 160, 3), dtype=np.uint8)  # dark, but textured
    detection = detect_bars(arr)
    assert detection.edges["top"] == 0
    assert detection.box.y == 0


def test_uniform_letterbox_is_removed() -> None:
    rng = np.random.default_rng(12)
    arr = rng.integers(60, 256, size=(200, 160, 3), dtype=np.uint8)
    arr[:30] = 0
    arr[170:] = 0
    detection = detect_bars(arr)
    assert detection.edges == {"top": 30, "bottom": 30, "left": 0, "right": 0}
    assert detection.box.as_dict() == {"x": 0, "y": 30, "w": 160, "h": 140}


def test_band_over_a_quarter_of_the_frame_is_refused_with_measurements() -> None:
    rng = np.random.default_rng(13)
    arr = rng.integers(60, 256, size=(200, 160, 3), dtype=np.uint8)
    arr[:51] = 0
    with pytest.raises(ValueError) as excinfo:
        detect_bars(arr)
    message = str(excinfo.value)
    assert "top=51" in message and "--box" in message

    exact = rng.integers(60, 256, size=(200, 160, 3), dtype=np.uint8)
    exact[:50] = 0
    assert detect_bars(exact).edges["top"] == 50  # exactly 25% is allowed


def test_remove_bars_refusal_is_a_validation_error_through_crop_media() -> None:
    rng = np.random.default_rng(14)
    arr = rng.integers(60, 256, size=(200, 160, 3), dtype=np.uint8)
    arr[:120] = 0
    client = _Client({"M-src": _row("O-src", _png_bytes(arr), "still.png", "image/png")})

    result = crop_media(client, project="P", ref="M-src", remove_bars=True)

    assert not result.ok and result.error.code == "validation_error"
    assert "--box" in result.error.message
    assert client.media.objects == {}  # nothing imported


def test_cli_warns_when_bar_threshold_is_given_with_box(capsys) -> None:
    client = _Client({"M-src": _row("O-src", _bars_png(), "still.png", "image/png")})

    rc = run_product_family(
        "media",
        ["crop", "--project", "P", "M-src", "--box", "0,20,320,150", "--bar-threshold", "10", "--dry-run"],
        client=client,
    )

    assert rc == 0
    assert "ignored with --box" in capsys.readouterr().err


# -- images -----------------------------------------------------------------


def test_exif_orientation_is_applied_before_cropping() -> None:
    arr = np.zeros((20, 40, 3), dtype=np.uint8)  # stored landscape, displayed portrait
    arr[:, :20] = (255, 0, 0)
    arr[:, 20:] = (0, 0, 255)
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise for display
    data = _png_bytes(arr, exif=exif)

    assert image_size(data) == (20, 40)
    out = crop_image_bytes(data, Box(0, 0, 20, 40))
    cropped = Image.open(io.BytesIO(out))
    assert cropped.size == (20, 40)
    assert cropped.getexif().get(0x0112) in (None, 1)  # orientation dropped
    pixels = np.asarray(cropped.convert("RGB"))
    assert tuple(pixels[0, 0]) == (255, 0, 0)  # the red half is now the top half
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    assert np.array_equal(pixels, np.asarray(expected.convert("RGB")))


def test_icc_profile_is_kept_on_crop() -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    arr = np.full((16, 16, 3), 90, dtype=np.uint8)
    data = _png_bytes(arr, icc_profile=profile)

    out = crop_image_bytes(data, Box(0, 0, 8, 8))

    assert Image.open(io.BytesIO(out)).info.get("icc_profile") == profile


def test_cmyk_source_is_converted_to_rgb_explicitly() -> None:
    out_buf = io.BytesIO()
    Image.new("CMYK", (40, 30), (0, 255, 0, 0)).save(out_buf, format="JPEG")

    out = crop_image_bytes(out_buf.getvalue(), Box(0, 0, 20, 10))

    decoded = Image.open(io.BytesIO(out))
    assert decoded.format == "PNG"
    assert decoded.mode == "RGB"
    assert decoded.size == (20, 10)


def test_unreadable_image_is_a_clean_validation_error() -> None:
    full = _bars_png()
    for payload in (b"not an image at all", full[:60]):
        client = _Client({"M-src": _row("O-src", payload, "still.png", "image/png")})
        result = crop_media(client, project="P", ref="M-src", box="0,0,4,4")
        assert not result.ok, payload[:12]
        assert result.error.code == "validation_error"
        assert "still image" in result.error.message


def test_animated_image_is_refused_with_a_clear_message() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(
        buf, format="PNG", save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")],
    )
    client = _Client({"M-anim": _row("O-anim", buf.getvalue(), "anim.png", "image/png")})

    result = crop_media(client, project="P", ref="M-anim", box="0,0,4,4", dry_run=True)

    assert not result.ok and result.error.code == "validation_error"
    assert "animated" in result.error.message
    assert "export one still frame" in result.error.message


# -- provenance and re-runs -------------------------------------------------


def test_remove_bars_then_box_with_same_rectangle_reuses_provenance() -> None:
    client = _Client({"M-src": _row("O-src", _bars_png(), "still.png", "image/png")})

    first = crop_media(client, project="P", ref="M-src", remove_bars=True)
    second = crop_media(client, project="P", ref="M-src", box="0,20,320,150")

    assert first.ok, first.error
    assert first.data["provenance_recorded"] == "new"
    assert second.ok, second.error
    assert second.data["provenance_recorded"] == "existing"
    assert len(client.media.relations) == 1
    assert client.media.import_keys[0] != client.media.import_keys[1]  # key covers the bars detail


def test_identical_rerun_with_default_key_succeeds() -> None:
    client = _Client({"M-src": _row("O-src", _bars_png(), "still.png", "image/png")})

    first = crop_media(client, project="P", ref="M-src", remove_bars=True)
    second = crop_media(client, project="P", ref="M-src", remove_bars=True)

    assert first.ok and second.ok, (first.error, second.error)
    assert client.media.relate_keys[0] == client.media.relate_keys[1]
    assert len(client.media.relations) == 1


def test_relate_failure_after_import_names_the_retry_key_and_retry_succeeds() -> None:
    client = _Client({"M-src": _row("O-src", _bars_png(), "still.png", "image/png")})
    client.media.relate_failure = ErrorObject("unavailable", "runtime went away", {})

    failed = crop_media(client, project="P", ref="M-src", box="0,20,320,150")

    key = client.media.import_keys[0]
    assert not failed.ok
    assert failed.error.details["idempotency_key"] == key
    assert f"--idempotency-key {key}" in failed.error.message
    assert "without provenance" in failed.error.message
    assert failed.error.details["imported_object_id"].startswith("sha256:")

    client.media.relate_failure = None
    retried = crop_media(client, project="P", ref="M-src", box="0,20,320,150", idempotency_key=key)

    assert retried.ok, retried.error
    assert retried.data["provenance_recorded"] == "new"
    assert len(client.media.relations) == 1


# -- noisy bands, JPEG ringing, no-bars refusal -----------------------------


def _picture(height: int, width: int, rng: np.random.Generator) -> np.ndarray:
    base = np.linspace(90, 200, width)[None, :, None]
    return np.clip(base + rng.normal(0, 12, (height, width, 3)), 0, 255).astype(np.uint8)


@pytest.mark.parametrize("quality", [75, 85, 95])
def test_jpeg_letterbox_detects_the_bars_within_one_row(quality: int) -> None:
    rng = np.random.default_rng(5)
    arr = _picture(600, 320, rng)
    arr[:121] = 0
    arr[600 - 122:] = 0
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=quality)
    rgb = np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))

    detection = detect_bars(rgb)

    assert abs(detection.edges["top"] - 121) <= 1, detection.edges
    assert abs(detection.edges["bottom"] - 122) <= 1, detection.edges
    assert detection.edges["left"] == 0 and detection.edges["right"] == 0


def test_grainy_bands_are_detected() -> None:
    rng = np.random.default_rng(6)
    arr = _picture(600, 320, rng).astype(np.float32)
    arr[:121] = np.clip(rng.normal(6, 2, (121, 320, 3)), 0, 255)
    arr[600 - 122:] = np.clip(rng.normal(6, 2, (122, 320, 3)), 0, 255)

    detection = detect_bars(arr.astype(np.uint8))

    assert detection.edges["top"] == 121 and detection.edges["bottom"] == 122


def test_no_bars_frame_refuses_import_unless_allowed() -> None:
    rng = np.random.default_rng(7)
    data = _png_bytes(_picture(120, 160, rng))
    client = _Client({"M-src": _row("O-src", data, "still.png", "image/png")})

    refused = crop_media(client, project="P", ref="M-src", remove_bars=True)
    assert not refused.ok and refused.error.code == "validation_error"
    assert "--allow-no-bars" in refused.error.message
    assert client.media.objects == {}

    dry = crop_media(client, project="P", ref="M-src", remove_bars=True, dry_run=True)
    assert dry.ok and dry.data["bars_found"] is False
    assert dry.data["box"] == {"x": 0, "y": 0, "w": 160, "h": 120}
    assert client.media.objects == {}

    allowed = crop_media(client, project="P", ref="M-src", remove_bars=True, allow_no_bars=True)
    assert allowed.ok, allowed.error
    assert allowed.data["bars_found"] is False
    assert len(client.media.objects) == 1


def test_even_only_difference_on_an_even_box_is_identical() -> None:
    client = _Client({"M-src": _row("O-src", _bars_png(), "still.png", "image/png")})

    first = crop_media(client, project="P", ref="M-src", box="0,20,320,150")
    second = crop_media(client, project="P", ref="M-src", box="0,20,320,150", even=True)

    assert first.ok, first.error
    assert second.ok, second.error
    assert second.data["provenance_recorded"] == "existing"
    assert len(client.media.relations) == 1


def test_icc_profile_is_dropped_when_pixels_convert_to_rgb() -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    buf = io.BytesIO()
    Image.new("CMYK", (40, 30), (0, 255, 0, 0)).save(buf, format="JPEG", icc_profile=profile)

    out = crop_image_bytes(buf.getvalue(), Box(0, 0, 20, 10))

    decoded = Image.open(io.BytesIO(out))
    assert decoded.mode == "RGB"
    assert "icc_profile" not in decoded.info


def test_mpo_still_uses_frame_zero() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(
        buf, format="MPO", save_all=True, append_images=[Image.new("RGB", (16, 16), "blue")],
    )
    client = _Client({"M-mpo": _row("O-mpo", buf.getvalue(), "photo.jpg", "image/jpeg")})

    result = crop_media(client, project="P", ref="M-mpo", box="0,0,4,4")

    assert result.ok, result.error
    pixel = np.asarray(Image.open(io.BytesIO(_imported(client))).convert("RGB"))[0, 0]
    assert pixel[0] > 200 and pixel[2] < 60  # red (frame 0), not blue


def test_multi_page_tiff_is_refused() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(
        buf, format="TIFF", save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")],
    )
    client = _Client({"M-tif": _row("O-tif", buf.getvalue(), "pages.tif", "image/tiff")})

    result = crop_media(client, project="P", ref="M-tif", box="0,0,4,4", dry_run=True)

    assert not result.ok and result.error.code == "validation_error"
    assert "not supported" in result.error.message
