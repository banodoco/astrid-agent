"""CPU proof that H3 binds and preserves a prepared middle source interval."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.generation import entries
from astrid.core.generation import engine_facts  # noqa: F401
from astrid.packs.h3_av.shared.compose import _sample_digest
from astrid.packs.h3_av.shared.request import normalize_request


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _source(path: Path) -> Path:
    # Deliberately nonconforming source; the requested [0.5, 3.0) interval must
    # be conformed to a zero-based, 60-frame source before the workflow binds it.
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=24000/1001:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", str(path),
    )
    return path


def _clip(path: Path, *, color: str, frequency: int, duration: str) -> Path:
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c={color}:s=1024x576:r=24:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={duration}",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    )
    return path


@pytest.mark.integration
@pytest.mark.ffmpeg
def test_h3_build_binds_middle_conform_and_after_outputs_verifies_protected_av(tmp_path: Path) -> None:
    import astrid.packs.vibecomfy.extensions  # noqa: F401  (register the contribution and hooks)

    entry = entries.find_entry("h3")
    source = _source(tmp_path / "source.mp4")
    public = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source", "range": [0.5, 3.0]},
        "output": {"duration": 2.5},
        "content": {"prompt": "Change the middle interval."},
        "changes": {
            "video": [{"during": [1.0, 2.0], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [],
        },
        "references": [],
        "overrides": {"seed": 7, "steps": 8},
    }
    request = normalize_request(public)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(public), encoding="utf-8")
    inputs = {"request": str(request_path), "asset_map": {"source": str(source)}}
    job_dir = tmp_path / "job"

    built = entries.build_entry(entry, inputs, job_dir)
    assert built is not None
    state = json.loads((job_dir / "h3" / "build-state.json").read_text(encoding="utf-8"))
    preparation = state["preparation"]
    source_conform = preparation["source_conform"]
    prepared_source = Path(state["source"])
    assert preparation["original_request_digest"] == request.digest
    assert preparation["original_request"]["source"]["range"] == [0.5, 3.0]
    assert source_conform["source"]["range_seconds"] == ["0.5", "3.0"]
    assert source_conform["output"]["frames"] == 60
    assert source_conform["output"]["range_seconds"] == [0, 2.5]
    assert hashlib.sha256(prepared_source.read_bytes()).hexdigest() == source_conform["output"]["sha256"]

    # The compiled archive unpacked by H3 build is the graph's exact source
    # binding, byte for byte with the prepared conform used by after_outputs.
    bound_source = Path(built["assets"]["source_video"])
    assert bound_source.read_bytes() == prepared_source.read_bytes()
    workflow = built["workflow"]
    api_graph = workflow.compile("api", run_inputs=built["bindings"])
    source_loader = next(row for row in api_graph.values() if row["class_type"] == "VHS_LoadVideoFFmpeg")
    assert source_loader["inputs"]["video"] == built["bindings"]["source_video"]

    hooks = entries.entry_hooks(entry)
    job = {**entries.entry_job(entry, inputs, job_dir, hooks=hooks),
           "manifest": {"models": [], "extra": {"nodes": []}}}
    generated = _clip(tmp_path / "generated.mp4", color="red", frequency=880, duration="2.5")
    engine = entries.engine_table()["comfyui"]
    # Build already ran above; finish is the same engine-owned after_outputs
    # hook used after a runtime output, without rebuilding the bounded conform.
    outputs = engine.finish(job, [str(generated)])

    assert outputs and Path(outputs[0]).is_file() and Path(outputs[0]) != generated
    verification = json.loads((job_dir / "h3" / "verify.json").read_text(encoding="utf-8"))
    assert verification["status"] == "verified"
    assert verification["preservation"]["status"] == "protected_sample_evidence"
    composition = json.loads((job_dir / "h3" / "compose" / "composition-manifest.json").read_text(encoding="utf-8"))
    candidate = Path(outputs[0])
    evidence = composition["preservation_evidence"]["protected_samples"]
    assert evidence["video"] and evidence["audio"]
    for stream in ("video", "audio"):
        for row in evidence[stream]:
            start, end = row["interval"]
            baseline_digest, baseline_count = _sample_digest(prepared_source, stream, start, end - start)
            assert row["source_sha256"] == baseline_digest
            assert row["sample_count"] <= baseline_count
            if stream == "video":
                candidate_digest, _ = _sample_digest(candidate, stream, start, end - start)
                assert candidate_digest == baseline_digest
            else:
                assert row["similarity"] >= 0.985
