from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.execution.thumbnails import ThumbnailError


def _creative_output(path: Path, *, media_type: str, ordinal: int = 0) -> dict[str, object]:
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "name": "generated",
        "path": str(path),
        "filename": path.name,
        "artifact_type": media_type,
        "media_type": media_type,
        "digest": digest,
        "size": path.stat().st_size,
        "role": "result",
        "is_primary": True,
        "ordinal": ordinal,
        "output_port": "generated_videos" if media_type.startswith("video/") else "generated_images",
        "group_key": "main",
        "variant_key": "original",
        "selector": {"group_key": "main", "variant_key": "original"},
    }


@pytest.mark.parametrize(
    "effect_type",
    [
        "generation.publish_v1",
        "generation.create_with_variant",
        "generation.variant.append",
    ],
)
def test_generation_thumbnail_output_for_image_preserves_creative_contract(
    tmp_path: Path, effect_type: str
) -> None:
    attempt = tmp_path / "attempt"
    output = attempt / "outputs" / "image.png"
    output.parent.mkdir(parents=True)
    Image.new("RGB", (640, 360), (10, 40, 90)).save(output)
    creative = _creative_output(output, media_type="image/png")

    host = object.__new__(GenericPackHost)
    thumbnails, diagnostics = host._generation_thumbnail_outputs(
        [creative],
        attempt_root=attempt,
        task_data={"expected_effect": {"effect_type": effect_type}},
    )

    assert len(thumbnails) == 1
    assert diagnostics == []
    thumbnail = thumbnails[0]
    assert thumbnail["name"] == "thumbnail"
    assert thumbnail["output_port"] == "thumbnail"
    assert thumbnail["role"] == "thumbnail"
    assert thumbnail["media_type"] == "image/jpeg"
    assert thumbnail["durability"] == "durable"
    assert "is_primary" not in thumbnail
    assert Path(str(thumbnail["path"])).is_file()
    assert thumbnail["provenance"] == {
        "thumbnail": {
            "source_object_ids": [creative["digest"]],
            "recipe_version": 1,
        }
    }
    assert creative["ordinal"] == 0
    assert creative["selector"] == {"group_key": "main", "variant_key": "original"}


@pytest.mark.ffmpeg
def test_generation_thumbnail_output_extracts_video_frame(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    output = attempt / "outputs" / "video.mp4"
    output.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=0.2",
            "-pix_fmt", "yuv420p", str(output),
        ],
        check=True,
    )
    creative = _creative_output(output, media_type="video/mp4")
    host = object.__new__(GenericPackHost)

    thumbnails, diagnostics = host._generation_thumbnail_outputs(
        [creative],
        attempt_root=attempt,
        task_data={"expected_effect": {"effect_type": "generation.publish_v1"}},
    )

    assert len(thumbnails) == 1
    assert diagnostics == []
    assert Path(str(thumbnails[0]["path"])).suffix == ".jpg"
    assert thumbnails[0]["provenance"]["thumbnail"]["source_object_ids"] == [
        creative["digest"]
    ]


def test_generation_thumbnail_failure_keeps_original_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "attempt"
    output = attempt / "outputs" / "video.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"not-a-video")
    creative = _creative_output(output, media_type="video/mp4")

    def fail(*_args: object, **_kwargs: object) -> object:
        raise ThumbnailError("decoder failed")

    monkeypatch.setattr(generic_host, "extract_thumbnail", fail)
    host = object.__new__(GenericPackHost)

    thumbnails, diagnostics = host._generation_thumbnail_outputs(
        [creative],
        attempt_root=attempt,
        task_data={"expected_effect": {"effect_type": "generation.variant.append"}},
    )

    assert thumbnails == []
    assert diagnostics == [
        {
            "code": "thumbnail_extraction_failed",
            "message": "decoder failed",
            "source_object_id": creative["digest"],
            "media_type": "video/mp4",
            "action": "inspect the source media and thumbnail decoder",
        }
    ]
    assert output.read_bytes() == b"not-a-video"
    assert creative["ordinal"] == 0


def test_generation_thumbnail_skips_nonvisual_creative_output(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    output = attempt / "outputs" / "audio.wav"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"audio")
    creative = _creative_output(output, media_type="audio/wav")
    host = object.__new__(GenericPackHost)

    thumbnails, diagnostics = host._generation_thumbnail_outputs(
        [creative],
        attempt_root=attempt,
        task_data={"expected_effect": {"effect_type": "generation.create_with_variant"}},
    )

    assert thumbnails == []
    assert diagnostics == []
    assert output.is_file()


def test_generation_thumbnail_deduplicates_equal_bytes_for_distinct_sources(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    first = output_root / "first.png"
    second = output_root / "second.png"
    Image.new("RGB", (640, 360), (10, 40, 90)).save(first)
    # Different source bytes, identical decoded pixels, and therefore one
    # content-addressed JPEG thumbnail.
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("source", "second")
    Image.new("RGB", (640, 360), (10, 40, 90)).save(second, pnginfo=pnginfo)
    assert first.read_bytes() != second.read_bytes()
    first_output = _creative_output(first, media_type="image/png", ordinal=0)
    second_output = _creative_output(second, media_type="image/png", ordinal=1)

    host = object.__new__(GenericPackHost)
    thumbnails, diagnostics = host._generation_thumbnail_outputs(
        [first_output, second_output],
        attempt_root=attempt,
        task_data={"expected_effect": {"effect_type": "generation.publish_v1"}},
    )

    assert len(thumbnails) == 1
    assert diagnostics == []
    assert thumbnails[0]["provenance"]["thumbnail"]["source_object_ids"] == [
        first_output["digest"], second_output["digest"]
    ]
    assert not (output_root / "thumbnail-0001.jpg").exists()


def test_generation_thumbnail_omits_digest_already_used_by_creative_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "attempt"
    output = attempt / "outputs" / "image.jpg"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"creative-jpeg")
    creative = _creative_output(output, media_type="image/jpeg")

    def reuse_creative(source: Path, destination: Path, _media_type: str) -> tuple[int, int]:
        destination.write_bytes(source.read_bytes())
        return (1, 1)

    monkeypatch.setattr(generic_host, "extract_thumbnail", reuse_creative)
    host = object.__new__(GenericPackHost)

    thumbnails, diagnostics = host._generation_thumbnail_outputs(
        [creative],
        attempt_root=attempt,
        task_data={"expected_effect": {"effect_type": "generation.publish_v1"}},
    )

    assert thumbnails == []
    assert diagnostics == []
    assert not (attempt / "outputs" / "thumbnail-0000.jpg").exists()
    assert output.read_bytes() == b"creative-jpeg"


def test_generation_thumbnail_skips_when_output_budget_has_no_headroom(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    output = attempt / "outputs" / "image.png"
    output.parent.mkdir(parents=True)
    Image.new("RGB", (640, 360), (10, 40, 90)).save(output)
    creative = _creative_output(output, media_type="image/png")

    host = object.__new__(GenericPackHost)
    thumbnails, diagnostics = host._generation_thumbnail_outputs(
        [creative],
        attempt_root=attempt,
        task_data={
            "expected_effect": {"effect_type": "generation.create_with_variant"},
            "storage_estimate": {"scratch_bytes": 0, "output_bytes": output.stat().st_size},
        },
    )

    assert thumbnails == []
    assert diagnostics == []
    assert output.is_file()
