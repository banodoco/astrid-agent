from __future__ import annotations

import argparse
import json
import hashlib
import os
from pathlib import Path

os.environ.setdefault("ASTRID_INTERNAL_INVOCATION", "1")

from astrid.core.pack.canonical import validate_canonical_pack
from astrid.packs.fal.actions.h3_video import run


PACK_ROOT = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "fal"


class FakeHttpClient:
    def register_secret(self, _value: str) -> None:
        return None


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    prompt_file = tmp_path / "prompt.txt"
    if not prompt_file.exists():
        prompt_file.write_text("A continuous observational shot.\n", encoding="utf-8")
    values: dict[str, object] = {
        "out": tmp_path / "out",
        "mode": "text-to-video",
        "prompt_file": prompt_file,
        "image_ref": [],
        "video_ref": [],
        "audio_ref": [],
        "duration": 15,
        "aspect_ratio": "16:9",
        "env_file": None,
        "timeout_seconds": 3600,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _downloader(_client: object, _url: str, destination: Path, _timeout: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"video")


def test_h3_action_declares_typed_video_port_and_manifest() -> None:
    action = validate_canonical_pack(PACK_ROOT).definition.actions["h3_video"]

    assert [
        (output["name"], output["type"], output.get("artifact_type"), output.get("path_template"))
        for output in action["outputs"]
    ] == [
        ("generated_videos", "file", "video/clip", None),
        ("video_manifest", "file", None, "{out}/manifest.json"),
    ]


def test_exact_overlong_prompt_fails_before_paid_submit(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("x" * 2001, encoding="utf-8")
    called = False

    def submitter(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    code, manifest = run.execute(
        _args(tmp_path, prompt_file=prompt_file),
        api_key="test-key",
        submitter=submitter,
    )

    assert code == 1
    assert called is False
    assert manifest["status"] == "failed"
    assert "at most 2000" in manifest["error"]
    assert (tmp_path / "out" / "inputs" / "prompt.txt").read_text().strip() == "x" * 2001


def test_reference_mode_uploads_ordered_images_and_downloads_video(tmp_path: Path) -> None:
    image1 = tmp_path / "one.png"
    image2 = tmp_path / "two.webp"
    image1.write_bytes(b"one")
    image2.write_bytes(b"two")
    captured: dict[str, object] = {}

    def uploader(_client: object, path: Path, _key: str) -> str:
        return f"https://example.test/{path.name}"

    def submitter(
        _client: object,
        endpoint: str,
        payload: dict[str, object],
        _key: str,
        **kwargs: object,
    ) -> dict[str, object]:
        captured.update(endpoint=endpoint, payload=payload, kwargs=kwargs)
        return {
            "request_id": "request-1",
            "video": {"url": "https://example.test/output.mp4"},
        }

    code, manifest = run.execute(
        _args(
            tmp_path,
            mode="reference-to-video",
            image_ref=[image1, image2],
            duration=15,
        ),
        client=FakeHttpClient(),
        api_key="test-key",
        uploader=uploader,
        submitter=submitter,
        downloader=_downloader,
    )

    assert code == 0
    assert manifest["status"] == "completed"
    assert captured["endpoint"] == "minimax/h3/reference-to-video"
    assert captured["payload"] == {
        "prompt": "A continuous observational shot.",
        "duration": 15,
        "resolution": "2K",
        "aspect_ratio": "16:9",
        "reference_image_urls": [
            "https://example.test/image1.png",
            "https://example.test/image2.webp",
        ],
    }
    assert (tmp_path / "out" / "outputs" / "minimax-h3-reference-to-video.mp4").is_file()
    saved = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert saved["outputs"] == [
        {
            "name": "generated_videos",
            "path": "outputs/minimax-h3-reference-to-video.mp4",
            "type": "file",
            "artifact_type": "video/clip",
            "media_type": "video/mp4",
            "ordinal": 0,
            "role": "result",
            "is_primary": True,
            "bytes": 5,
            "content_hash": "sha256:"
            + hashlib.sha256(b"video").hexdigest(),
        }
    ]
    assert [item["reference_label"] for item in saved["inputs"]["ordered_artifacts"]] == [
        "Image 1",
        "Image 2",
    ]


def test_text_mode_uses_exact_h3_endpoint_and_fixed_2k(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def submitter(
        _client: object,
        endpoint: str,
        payload: dict[str, object],
        _key: str,
        **kwargs: object,
    ) -> dict[str, object]:
        captured.update(endpoint=endpoint, payload=payload, kwargs=kwargs)
        return {"video": {"url": "https://example.test/output.mp4"}}

    code, _manifest = run.execute(
        _args(tmp_path, duration=12, aspect_ratio="4:3"),
        client=FakeHttpClient(),
        api_key="test-key",
        submitter=submitter,
        downloader=_downloader,
    )

    assert code == 0
    assert captured["endpoint"] == "minimax/h3/text-to-video"
    assert captured["payload"] == {
        "prompt": "A continuous observational shot.",
        "duration": 12,
        "resolution": "2K",
        "aspect_ratio": "4:3",
    }
