"""``astrid media crop``: box crop, bar detection, video crop, provenance, dry run."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from astrid.core.cli.domain_product import run_product_family
from astrid.core.media import ffprobe_metadata_strict
from astrid.core.media_crop import (
    Box,
    crop_image_bytes,
    crop_media,
    detect_bars,
    parse_box,
)
from astrid.sdk.contracts import DomainResult, ErrorObject

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")


def _png(width: int, height: int, *, top: int = 0, bottom: int = 0, seed: int = 7) -> bytes:
    """Synthetic picture with pure-black bars; content is bright noise."""
    rng = np.random.default_rng(seed)
    arr = rng.integers(60, 256, size=(height, width, 3), dtype=np.uint8)
    arr[:top] = 0
    if bottom:
        arr[height - bottom:] = 0
    out = io.BytesIO()
    Image.fromarray(arr, "RGB").save(out, format="PNG")
    return out.getvalue()


class _FakeMedia:
    def __init__(self, rows: dict[str, tuple[dict[str, Any], bytes]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.imported: dict[str, bytes] = {}

    def show(self, project, ref):
        self.calls.append(("show", {"project": project, "ref": ref}))
        if ref not in self.rows:
            return DomainResult.failure(ErrorObject("not_found", "missing", {}))
        return DomainResult.success(dict(self.rows[ref][0]))

    def read_bytes(self, object_id):
        self.calls.append(("read_bytes", {"object_id": object_id}))
        for row, data in self.rows.values():
            if row["object_id"] == object_id:
                return data
        raise AssertionError(object_id)

    def import_file(self, *, project, path, realm="managed_local", idempotency_key=None):
        self.calls.append(("import_file", {"project": project, "name": Path(path).name, "key": idempotency_key}))
        data = Path(path).read_bytes()
        object_id = f"O-new-{len(self.imported) + 1}"
        self.imported[object_id] = data
        return DomainResult.success(
            {"object_id": object_id, "digest": f"d-{object_id}", "filename": Path(path).name},
            idempotency_key=idempotency_key or "",
        )

    def relate(self, project, *, from_object_id, to_object_id, kind, metadata=None, idempotency_key=None):
        self.calls.append(("relate", {
            "project": project, "from_object_id": from_object_id, "to_object_id": to_object_id,
            "kind": kind, "metadata": metadata, "idempotency_key": idempotency_key,
        }))
        return DomainResult.success({"from": from_object_id, "to": to_object_id, "kind": kind})


class _FakeClient:
    def __init__(self, rows: dict[str, tuple[dict[str, Any], bytes]]) -> None:
        self.media = _FakeMedia(rows)


def _image_row(object_id: str, data: bytes, name: str = "still.png") -> tuple[dict[str, Any], bytes]:
    import hashlib

    return (
        {"object_id": object_id, "digest": hashlib.sha256(data).hexdigest(), "filename": name, "media_type": "image/png"},
        data,
    )


def _verbs(client: _FakeClient) -> list[str]:
    return [verb for verb, _ in client.media.calls]


# -- box and detection ------------------------------------------------------


def test_parse_box_and_bounds() -> None:
    assert parse_box("1, 2,3,4") == Box(1, 2, 3, 4)
    with pytest.raises(ValueError):
        parse_box("1,2,3")
    with pytest.raises(ValueError):
        parse_box("1,2,x,4")
    with pytest.raises(ValueError):
        Box(0, 0, 10, 10).check_within(9, 10)
    assert Box(0, 0, 1672, 941).even() == Box(0, 0, 1672, 940)


def test_box_crop_of_image_is_exact_lossless_region() -> None:
    arr = np.arange(40 * 64 * 3, dtype=np.uint32).reshape(40, 64, 3) % 256
    src = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8), "RGB").save(src, format="PNG")
    out = crop_image_bytes(src.getvalue(), Box(5, 6, 20, 10))
    cropped = np.asarray(Image.open(io.BytesIO(out)).convert("RGB"))
    assert cropped.shape == (10, 20, 3)
    assert np.array_equal(cropped, arr[6:16, 5:25].astype(np.uint8))


def test_detect_bars_exact_box_on_letterboxed_1672x941() -> None:
    data = _png(1672, 941, top=121, bottom=122)
    rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    detection = detect_bars(rgb)
    assert detection.threshold == 16
    assert detection.box == Box(0, 121, 1672, 941 - 121 - 122)
    assert detection.edges == {"top": 121, "bottom": 122, "left": 0, "right": 0}


def test_detect_bars_no_bars_keeps_full_frame() -> None:
    data = _png(320, 200)
    rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    detection = detect_bars(rgb)
    assert detection.box == Box(0, 0, 320, 200)
    assert detection.edges == {"top": 0, "bottom": 0, "left": 0, "right": 0}


def test_detect_bars_refuses_when_nothing_would_remain() -> None:
    rgb = np.zeros((50, 50, 3), dtype=np.uint8)
    with pytest.raises(ValueError):
        detect_bars(rgb)


# -- image crop through the import path ------------------------------------


def test_remove_bars_imports_new_item_with_provenance_and_leaves_source() -> None:
    data = _png(1672, 941, top=121, bottom=122)
    client = _FakeClient({"M-src": _image_row("O-src", data)})
    before = dict(client.media.rows)

    result = crop_media(client, project="P", ref="M-src", remove_bars=True)

    assert result.ok, result.error
    assert _verbs(client) == ["show", "read_bytes", "import_file", "relate"]
    imported = client.media.imported["O-new-1"]
    assert Image.open(io.BytesIO(imported)).size == (1672, 698)
    relate = client.media.calls[-1][1]
    assert relate["from_object_id"] == "O-new-1"
    assert relate["to_object_id"] == "O-src"
    assert relate["kind"] == "derived_from"
    assert relate["metadata"]["source_digest"] == before["M-src"][0]["digest"]
    assert relate["metadata"]["box"] == {"x": 0, "y": 121, "w": 1672, "h": 698}
    assert relate["metadata"]["bars"]["threshold"] == 16
    assert client.media.calls[2][1]["name"] == "still-crop.png"
    assert client.media.rows["M-src"] == before["M-src"]  # source row untouched
    assert result.data["box"] == {"x": 0, "y": 121, "w": 1672, "h": 698}


def test_no_bars_image_keeps_full_box() -> None:
    client = _FakeClient({"M-src": _image_row("O-src", _png(128, 96))})
    result = crop_media(client, project="P", ref="M-src", remove_bars=True, dry_run=True)
    assert result.ok
    assert result.data["box"] == {"x": 0, "y": 0, "w": 128, "h": 96}
    assert result.data["detected"]["edges"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}


def test_dry_run_writes_nothing() -> None:
    client = _FakeClient({"M-src": _image_row("O-src", _png(1672, 941, top=121, bottom=122))})
    result = crop_media(client, project="P", ref="M-src", remove_bars=True, dry_run=True, even=True)
    assert result.ok
    assert result.data["dry_run"] is True
    assert result.data["output"] == {"width": 1672, "height": 698}
    assert _verbs(client) == ["show", "read_bytes"]
    assert client.media.imported == {}


def test_box_must_be_inside_source_and_exclusive_with_bars() -> None:
    client = _FakeClient({"M-src": _image_row("O-src", _png(64, 64))})
    out_of_bounds = crop_media(client, project="P", ref="M-src", box="0,0,65,10")
    assert not out_of_bounds.ok and out_of_bounds.error.code == "validation_error"
    both = crop_media(client, project="P", ref="M-src", box="0,0,10,10", remove_bars=True)
    assert not both.ok and both.error.code == "validation_error"
    assert "import_file" not in _verbs(client)


def test_even_rounds_box_down() -> None:
    client = _FakeClient({"M-src": _image_row("O-src", _png(101, 61))})
    result = crop_media(client, project="P", ref="M-src", box="0,0,101,61", even=True, dry_run=True)
    assert result.ok
    assert result.data["box"] == {"x": 0, "y": 0, "w": 100, "h": 60}


# -- video crop -------------------------------------------------------------


def _make_letterboxed_video(path: Path) -> None:
    """1 s, 25 fps, 160x120 white picture padded with 20 px black bars, plus a sine track."""
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=white:s=160x120:r=25:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-vf", "pad=160:160:0:20:black",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@needs_ffmpeg
def test_video_crop_keeps_duration_fps_and_audio(tmp_path: Path) -> None:
    src = tmp_path / "clip.mp4"
    _make_letterboxed_video(src)
    before = ffprobe_metadata_strict(src)
    client = _FakeClient({"M-vid": (
        {"object_id": "O-vid", "digest": "dv", "filename": "clip.mp4", "media_type": "video/mp4"},
        src.read_bytes(),
    )})

    result = crop_media(client, project="P", ref="M-vid", remove_bars=True, even=True)

    assert result.ok, result.error
    assert result.data["box"] == {"x": 0, "y": 20, "w": 160, "h": 120}
    out = tmp_path / "out.mp4"
    out.write_bytes(client.media.imported["O-new-1"])
    after = ffprobe_metadata_strict(out)
    assert (after.width, after.height) == (160, 120)
    assert after.fps_rational == before.fps_rational
    assert abs((after.duration_seconds or 0) - (before.duration_seconds or 0)) < 0.15
    audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert audio, "audio stream must be kept"


@needs_ffmpeg
def test_video_odd_dimensions_need_even_flag(tmp_path: Path) -> None:
    src = tmp_path / "clip.mp4"
    _make_letterboxed_video(src)
    client = _FakeClient({"M-vid": (
        {"object_id": "O-vid", "digest": "dv", "filename": "clip.mp4", "media_type": "video/mp4"},
        src.read_bytes(),
    )})
    result = crop_media(client, project="P", ref="M-vid", box="0,20,159,120")
    assert not result.ok and result.error.code == "validation_error"
    assert "import_file" not in _verbs(client)


# -- CLI surface ------------------------------------------------------------


def test_cli_crop_dry_run_json_envelope(capsys) -> None:
    client = _FakeClient({"M-src": _image_row("O-src", _png(1672, 941, top=121, bottom=122))})
    rc = run_product_family(
        "media", ["crop", "--project", "P", "M-src", "--remove-bars", "--dry-run", "--json"], client=client,
    )
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) >= {"ok", "data", "error", "receipt", "idempotency_key"}
    assert envelope["ok"] is True
    assert envelope["data"]["box"] == {"x": 0, "y": 121, "w": 1672, "h": 698}
    assert _verbs(client) == ["show", "read_bytes"]


def test_cli_crop_requires_box_or_remove_bars() -> None:
    client = _FakeClient({"M-src": _image_row("O-src", _png(8, 8))})
    with pytest.raises(SystemExit) as excinfo:
        run_product_family("media", ["crop", "--project", "P", "M-src"], client=client)
    assert excinfo.value.code == 2
    assert client.media.calls == []


# -- side bars: pillarbox pairs only ------------------------------------------


def _pillar(width: int, height: int, *, left: int, right: int, seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    arr = rng.integers(60, 256, size=(height, width, 3), dtype=np.uint8)
    arr[:, :left] = 0
    if right:
        arr[:, width - right:] = 0
    return arr


def test_dark_wall_on_left_only_is_picture_not_a_pillarbox() -> None:
    # 78 px of dark wall down the left edge of a real frame, nothing on the right.
    rgb = _pillar(320, 200, left=78, right=0)
    detection = detect_bars(rgb)
    assert detection.edges == {"top": 0, "bottom": 0, "left": 0, "right": 0}
    assert detection.box == Box(0, 0, 320, 200)


def test_dark_wall_on_left_only_is_not_cropped_by_remove_bars_import() -> None:
    data = _png(320, 200)
    arr = np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).copy()
    arr[:, :78] = 0
    out = io.BytesIO()
    Image.fromarray(arr, "RGB").save(out, format="PNG")
    client = _FakeClient({"M-src": _image_row("O-src", out.getvalue())})
    result = crop_media(client, project="P", ref="M-src", remove_bars=True)
    assert not result.ok and result.error.code == "validation_error"
    assert "import_file" not in _verbs(client)


def test_true_pillarbox_crops_both_sides() -> None:
    rgb = _pillar(320, 200, left=60, right=60)
    detection = detect_bars(rgb)
    assert detection.edges == {"top": 0, "bottom": 0, "left": 60, "right": 60}
    assert detection.box == Box(60, 0, 200, 200)


def test_pillarbox_sides_within_tolerance_are_paired() -> None:
    rgb = _pillar(320, 200, left=60, right=63)
    detection = detect_bars(rgb)
    assert detection.edges["left"] == 60 and detection.edges["right"] == 63


def test_unequal_side_bands_beyond_tolerance_are_not_cropped() -> None:
    rgb = _pillar(320, 200, left=60, right=70)
    detection = detect_bars(rgb)
    assert detection.edges["left"] == 0 and detection.edges["right"] == 0


def test_top_and_bottom_keep_their_rule_with_side_pairing() -> None:
    rgb = _pillar(320, 200, left=60, right=60)
    rgb[:20] = 0
    detection = detect_bars(rgb)
    assert detection.edges == {"top": 20, "bottom": 0, "left": 60, "right": 60}


def test_one_sided_wide_dark_band_does_not_trip_the_edge_cap() -> None:
    # 40% of the width is dark on the left only: previously refused as a bar wider than 25%.
    rgb = _pillar(320, 200, left=128, right=0)
    detection = detect_bars(rgb)
    assert detection.edges["left"] == 0
