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
    assert thumbnail["digest"] == "sha256:" + hashlib.sha256(
        Path(str(thumbnail["path"])).read_bytes()
    ).hexdigest()
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


def test_output_upload_carries_harvest_digest_and_runtime_checks_mutation(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from astrid.core._shared import result_manifest
    from astrid.core.execution.generic_host import RuntimeProtocolClient

    root = tmp_path / "attempt"
    output_root = root / "outputs"
    output_root.mkdir(parents=True)
    path = output_root / "output.bin"
    data = b"one output" * 100
    path.write_bytes(data)
    declared = SimpleNamespace(name="result", path_template="output.bin", artifact_type="application/octet-stream")
    record = SimpleNamespace(definition=SimpleNamespace(outputs=(declared,), metadata={}))
    observed = []
    original_file_hash = result_manifest.sha256_file
    monkeypatch.setattr(result_manifest, "sha256_file", lambda item: (observed.append(Path(item)), original_file_hash(item))[1])
    descriptors = result_manifest.harvest_staged_outputs(output_root, declared_outputs=(declared,))
    assert observed == [path]

    host = object.__new__(GenericPackHost)
    protocol = object.__new__(RuntimeProtocolClient)
    protocol.executor_id = "worker"
    protocol._claimed_runtime_epoch = lambda _attempt: 1
    accepted = []

    def ingest(data, *, upload_binding, **kwargs):
        # Model the Runtime's independent upload-binding check, not host trust.
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if upload_binding["digest"] != digest or upload_binding["size"] != len(data):
            raise ValueError("Runtime rejected changed bytes")
        accepted.append(digest)
        return SimpleNamespace(digest=digest, size=len(data))

    protocol.generated = SimpleNamespace(ingest_object=ingest)
    protocol.INLINE_SETTLEMENT_OUTPUTS = False
    host.client = protocol
    # If the host rehashes while typing or uploading, this focused test fails.
    monkeypatch.setattr(generic_host.hashlib, "file_digest", lambda *_a, **_k: pytest.fail("redundant host file hash"))
    original_sha256 = hashlib.sha256
    byte_hashes = []
    def count_hash(value=b"", *args, **kwargs):
        if value == data:
            byte_hashes.append(value)
        return original_sha256(value, *args, **kwargs)
    monkeypatch.setattr(generic_host.hashlib, "sha256", count_hash)
    typed = host._typed_outputs(record, descriptors, root, defer_content_check_to_runtime=True)
    args = dict(project_id="project", run_id="run", task_id="task", attempt_id="attempt",
                lease_id="lease", fence=1, runtime_epoch=1)
    uploaded = host._upload_outputs(typed, **args)
    assert uploaded[0]["digest"] == typed[0]["digest"] == accepted[0]
    assert byte_hashes == [data]  # only the simulated Runtime boundary
    path.write_bytes(b"X" * len(data))
    with pytest.raises(ValueError, match="Runtime rejected changed bytes"):
        host._upload_outputs(typed, **args)
    assert len(accepted) == 1
    path.write_bytes(data)
    protocol.generated = SimpleNamespace(ingest_object=lambda *_a, **_k: SimpleNamespace(
        digest="sha256:" + "0" * 64, size=len(data)))
    with pytest.raises(generic_host.HostError, match="different canonical digest"):
        host._upload_outputs(typed, **args)


def test_inline_runtime_rejects_output_changed_after_harvest(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from astrid.core._shared.result_manifest import harvest_staged_outputs
    from astrid.core.execution.generic_host import RuntimeProtocolClient
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.errors import ConflictError

    root = tmp_path / "attempt"
    output_root = root / "outputs"
    output_root.mkdir(parents=True)
    path = output_root / "output.bin"
    path.write_bytes(b"original")
    declared = SimpleNamespace(name="result", path_template="output.bin", artifact_type="application/octet-stream")
    record = SimpleNamespace(definition=SimpleNamespace(outputs=(declared,), metadata={}))
    descriptors = harvest_staged_outputs(output_root, declared_outputs=(declared,))
    host = object.__new__(GenericPackHost)
    host.client = object.__new__(RuntimeProtocolClient)
    typed = host._typed_outputs(record, descriptors, root, defer_content_check_to_runtime=True)
    path.write_bytes(b"modified")  # Same length; timestamp and size cannot prove custody.
    outputs = host._upload_outputs(typed, project_id=None)

    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm)
    try:
        with pytest.raises(ConflictError, match="output content hash does not match declared digest"):
            service._stage_outputs("attempt-1", outputs)
    finally:
        service.close()
