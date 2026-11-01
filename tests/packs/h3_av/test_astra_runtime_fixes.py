from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.h3_av.actions.transform.run import (
    _retrieve_generation_outputs,
    _write_generated_bundle,
)
from astrid.packs.h3_av.shared.compose import compose_candidate
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.vibecomfy import invocation_preflight


def _request():
    return normalize_request(
        {
            "version": 1,
            "operation": "edit",
            "source": {"asset": "source", "range": [0, 1]},
            "output": {"duration": 2},
            "content": {"prompt": "replace the generated second"},
            "changes": {
                "video": [{"during": [1, 2], "area": {"full_frame": True}, "action": "generate"}],
                "audio": [{"during": [1, 2], "action": "generate"}],
            },
            "references": [],
            "overrides": {},
        }
    )


def test_preflight_accepts_typed_scalars_and_internal_json_without_media_filename_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workflow_path = tmp_path / "workflow.py"
    workflow_path.write_text("workflow = 'fixture'", encoding="utf-8")

    class Workflow:
        inputs = {
            "source_video": SimpleNamespace(node_id="7", field="video", media_semantics=None),
            "seed": SimpleNamespace(node_id="8", field="value", media_semantics=None),
            "mask_keyframes": SimpleNamespace(node_id="9", field="keyframes", media_semantics=None),
            "audio_intervals": SimpleNamespace(node_id="9", field="audio_mask", media_semantics=None),
        }
        outputs = [SimpleNamespace(node_id="3")]
        edges = [
            SimpleNamespace(from_node="2", to_node="3"),
            SimpleNamespace(from_node="7", to_node="3"),
        ]

        def compile(self, _kind, *, run_inputs):
            return {
                "2": {"class_type": "Prompt", "inputs": {"prompt": "hello"}},
                "3": {"class_type": "Output", "inputs": {}},
                "7": {"class_type": "LoadVideo", "inputs": {"video": run_inputs["source_video"]}},
                "9": {
                    "class_type": "LanPaint_AVEncode",
                    "inputs": {"mask": ["164", 1], "audio_mask": run_inputs["audio_intervals"]},
                },
            }

    loaded = SimpleNamespace(
        resolved=SimpleNamespace(
            workflow=Workflow(),
            workflow_identity="lanpaint",
            revision_id="rev-1",
            semantic_digest="sha256:" + "1" * 64,
        ),
        workflow_content_digest="sha256:" + "2" * 64,
    )
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.load_workflow_path",
        lambda *_args, **_kwargs: loaded,
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    receipt = invocation_preflight.preflight_invocation(
        workflow_path,
        run_inputs={
            "source_video": "source.mp4",
            "seed": 7,
            "mask_keyframes": '{"0":"mask_preserve.png"}',
            "audio_intervals": "[]",
        },
        source_video_path=source,
        expected_source_node="7",
    )
    assert receipt["run_inputs"]["seed"] == 7
    assert receipt["run_inputs"]["audio_intervals"] == "[]"


class _Media:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def read_bytes(self, object_id: str) -> bytes:
        return self.objects[object_id]


def _managed_row(role: str, payload: bytes) -> dict[str, object]:
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    return {
        "name": "vibecomfy_run",
        "output_port": "vibecomfy_run",
        "producer": {"output_port": role, "producer_output_id": f"vibecomfy:{role}:0"},
        "object_id": digest,
        "digest": digest,
        "size": len(payload),
        "filename": f"candidate.{ 'mp4' if role == 'video' else 'flac' }",
        "role": "result",
    }


def test_lanpaint_retrieval_requires_and_preserves_video_audio_roles(tmp_path: Path) -> None:
    video = b"video-bytes"
    audio = b"audio-bytes"
    rows = [_managed_row("video", video), _managed_row("audio", audio)]
    result = SimpleNamespace(outputs={"managed_outputs": rows}, raw_result={})
    paths = _retrieve_generation_outputs(
        SimpleNamespace(media=_Media({row["object_id"]: payload for row, payload in zip(rows, (video, audio))})),
        result,
        tmp_path,
    )
    assert set(paths) == {"video", "audio"}
    bundle = _write_generated_bundle(paths, tmp_path / "generated.zip")
    with pytest.raises(RuntimeError, match="missing required output role.*audio"):
        _retrieve_generation_outputs(
            SimpleNamespace(media=_Media({rows[0]["object_id"]: video})),
            SimpleNamespace(outputs={"managed_outputs": [rows[0]]}, raw_result={}),
            tmp_path / "missing",
        )
    assert bundle.is_file()
    manifest = json.loads(__import__("zipfile").ZipFile(bundle).read("manifest.json"))
    assert {item["role"] for item in manifest["outputs"]} == {"video", "audio"}


@pytest.mark.ffmpeg
def test_paired_lanpaint_bundle_reaches_composition_with_both_streams(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.flac"
    for output, color, frequency, duration in (
        (source, "blue", 440, 1),
        (video, "red", 880, 2),
    ):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=32x32:r=8", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000", "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(output)],
            check=True,
        )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000", "-t", "2", "-c:a", "flac", str(audio)],
        check=True,
    )
    preparation = prepare_request(_request(), asset_map={"source": str(source)})
    video_row = {"sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "object_id": "sha256:" + hashlib.sha256(video.read_bytes()).hexdigest(), "producer_output_id": "vibecomfy:video:0", "output_port": "video"}
    audio_row = {"sha256": hashlib.sha256(audio.read_bytes()).hexdigest(), "object_id": "sha256:" + hashlib.sha256(audio.read_bytes()).hexdigest(), "producer_output_id": "vibecomfy:audio:1", "output_port": "audio"}
    bundle = _write_generated_bundle({"video": (video, video_row), "audio": (audio, audio_row)}, tmp_path / "generated.zip")
    composition = compose_candidate(preparation=preparation, generated=bundle, source=source, out_dir=tmp_path / "composition")
    assert composition["composition"]["output_roles"] == ["video", "audio"]
    assert {item["role"] for item in composition["generated_outputs"]} == {"video", "audio"}
