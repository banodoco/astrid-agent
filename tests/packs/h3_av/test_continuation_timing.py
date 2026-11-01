from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.h3_av.actions.transform.run import (
    _retrieve_muxed_generation,
    _write_generated_bundle,
)
from astrid.packs.h3_av.shared.compose import (
    CompositionError,
    _sample_digest,
    compose_candidate,
)
from astrid.packs.h3_av.actions.compile.compile import compile_preparation
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.shared.timing import ContinuationTimingError, plan_continuation
from astrid.packs.h3_av.actions.verify.verify import verify_candidate


def _request():
    return normalize_request(
        {
            "version": 1,
            "operation": "continue",
            "source": {"asset": "source", "range": [0, 5]},
            "output": {"duration": 6},
            "content": {"prompt": "Continue the speaker into the requested end state."},
            "changes": {
                "video": [
                    {
                        "during": [5, 6],
                        "area": {"full_frame": True},
                        "action": "generate",
                    }
                ],
                "audio": [{"during": [5, 6], "action": "generate"}],
            },
            "references": [],
            "overrides": {},
        }
    )


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def _av(path: Path, *, frames: int, color: str, frequency: int) -> None:
    duration = frames / 24
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=64x48:r=24",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000",
            "-frames:v",
            str(frames),
            "-t",
            str(duration),
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )


def _video(path: Path, *, frames: int, color: str) -> None:
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=64x48:r=24",
            "-frames:v",
            str(frames),
            "-c:v",
            "ffv1",
            str(path),
        ]
    )


def _audio(path: Path, *, frames: int, frequency: int) -> None:
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000",
            "-t",
            str(frames / 24),
            "-c:a",
            "flac",
            str(path),
        ]
    )


def test_timing_separates_preserved_context_from_net_new_frames() -> None:
    timing = plan_continuation(source_end=5, output_duration=6)

    assert timing.source_frames == 120
    assert timing.requested_new_frames == 24
    assert timing.context_frames == 39
    assert timing.raw_extension_frames == 73
    assert timing.generated_capacity_frames == 34
    assert timing.trim_tail_frames == 10
    assert timing.expected_graph_output_frames == 154
    assert timing.workflow_duration == 73 / 24


def test_timing_rejects_zero_or_subframe_suffix_before_execution() -> None:
    with pytest.raises(ContinuationTimingError, match="at least one net new frame"):
        plan_continuation(source_end=5, output_duration=5)
    with pytest.raises(ContinuationTimingError, match="24 fps frame grid"):
        plan_continuation(source_end=5, output_duration=5.01)


def test_compiler_exports_raw_h3_duration_not_requested_suffix_duration(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-source")
    preparation = prepare_request(_request(), asset_map={"source": str(source)})

    compilation = compile_preparation(preparation, out_dir=tmp_path / "compiled")

    assert compilation["workflow_inputs"]["duration"] == 73 / 24
    assert compilation["workflow_inputs"]["duration"] != 1.0
    assert compilation["workflow_inputs"]["source_frames"] == 120
    assert compilation["continuation_timing"]["expected_graph_output_frames"] == 154
    assert compilation["continuation_timing"]["requested_output_frames"] == 144
    workflow_source = Path(compilation["workflow"]["workflow.py"]["path"]).read_text(
        encoding="utf-8"
    )
    assert "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17" in workflow_source
    assert "('duration', '102', 'values.a', duration, None)" in workflow_source


@pytest.mark.ffmpeg
def test_muxed_continuation_composes_real_suffix_and_rejects_prior_noop(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    generated = tmp_path / "generated-full-timeline.mkv"
    _av(source, frames=120, color="blue", frequency=440)
    _av(generated, frames=154, color="red", frequency=880)
    preparation = prepare_request(_request(), asset_map={"source": str(source)})

    with pytest.raises(CompositionError, match="no net new frames"):
        compose_candidate(
            preparation=preparation,
            generated=source,
            source=source,
            out_dir=tmp_path / "noop",
        )

    composition = compose_candidate(
        preparation=preparation,
        generated=generated,
        source=source,
        out_dir=tmp_path / "composed",
    )
    report = verify_candidate(
        preparation=preparation,
        composition=composition,
        source=source,
    )

    candidate = Path(composition["candidate"]["path"])
    assert composition["composition"]["output_roles"] == ["muxed_av"]
    assert composition["coverage"]["generated"]["video"]["frames"] == 154
    assert composition["coverage"]["candidate"]["video"]["frames"] == 144
    assert composition["coverage"]["candidate"]["requested_duration"] == 6
    assert report["coverage"]["video"]["frames"] == 144
    assert report["preservation"]["status"] == "protected_sample_evidence"
    source_blue, _ = _sample_digest(source, "video", 0, 1)
    suffix_red, _ = _sample_digest(candidate, "video", 5, 1)
    assert suffix_red != source_blue


@pytest.mark.ffmpeg
def test_separate_video_audio_outputs_remain_supported_for_continuation(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    video = tmp_path / "generated-video.mkv"
    audio = tmp_path / "generated-audio.flac"
    _av(source, frames=120, color="blue", frequency=440)
    _video(video, frames=154, color="red")
    _audio(audio, frames=154, frequency=880)
    preparation = prepare_request(_request(), asset_map={"source": str(source)})

    def row(role: str, path: Path) -> dict[str, str]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "sha256": digest,
            "object_id": f"sha256:{digest}",
            "output_port": role,
        }

    bundle = _write_generated_bundle(
        {"video": (video, row("video", video)), "audio": (audio, row("audio", audio))},
        tmp_path / "generated-av.zip",
    )
    composition = compose_candidate(
        preparation=preparation,
        generated=bundle,
        source=source,
        out_dir=tmp_path / "composed",
    )
    report = verify_candidate(
        preparation=preparation,
        composition=composition,
        source=source,
    )

    assert composition["composition"]["output_roles"] == ["video", "audio"]
    assert composition["coverage"]["candidate"]["video"]["frames"] == 144
    assert report["status"] == "verified"


def test_muxed_runtime_retrieval_keeps_one_result_without_inventing_audio_role(tmp_path: Path) -> None:
    payload = b"muxed-av"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    row = {
        "name": "vibecomfy_run",
        "object_id": digest,
        "digest": digest,
        "size": len(payload),
        "filename": "continuation.mp4",
        "role": "result",
        "producer": {"output_port": "continuation", "producer_output_id": "vibecomfy:continuation:0"},
    }

    class Media:
        def read_bytes(self, object_id: str) -> bytes:
            assert object_id == digest
            return payload

    path, settled = _retrieve_muxed_generation(
        SimpleNamespace(media=Media()),
        SimpleNamespace(capability_id="vibecomfy.run", outputs={"managed_outputs": [row]}, raw_result={}),
        tmp_path,
    )

    assert path.read_bytes() == payload
    assert settled["object_id"] == digest
