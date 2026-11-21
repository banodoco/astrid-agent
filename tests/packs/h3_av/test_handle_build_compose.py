"""A digest handle crosses H3 build and CPU composition with source lineage intact."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from astrid.sdk.generation import GenerationFacade


def _source_markers_and_pulse(path: Path) -> None:
    """Write 96 independently marked video frames and a known middle-range audio pulse."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i",
         "nullsrc=s=1024x576:r=24:d=4,format=yuv444p,geq=lum='20+2*N':cb=128:cr=128",
         "-f", "lavfi", "-i",
         "aevalsrc=0.15*sin(2*PI*440*t)+0.6*sin(2*PI*880*t)*between(t\\,1.5\\,1.75):s=48000:d=4",
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "0", "-pix_fmt", "yuv444p", "-c:a", "aac", "-ar", "48000", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )


def _generated(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "color=c=white:s=1024x576:r=24:d=2", "-frames:v", "48", "-c:v", "ffv1",
         "-pix_fmt", "yuv444p", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )


def _gray_markers(path: Path) -> np.ndarray:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0",
         "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True, capture_output=True, timeout=30,
    )
    frame_size = 1024 * 576
    assert len(result.stdout) % frame_size == 0
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, 576, 1024)


def _pulse_peak(path: Path, start: float) -> float:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
        check=True, capture_output=True, timeout=30,
    )
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    window = samples[round(start * 48000):round(start * 48000) + 4800]
    assert len(window) == 4800
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    frequencies = np.fft.rfftfreq(len(window), d=1 / 48000)
    band = (frequencies >= 300) & (frequencies <= 1200)
    return float(frequencies[band][np.argmax(spectrum[band])])


class _ProjectMedia:
    def __init__(self, digest: str, source: Path):
        self.digest = digest
        self.source = source
        self.opened: list[Path] = []

    def show(self, project: str, digest: str):
        assert project == "h3-handle-project"
        if digest != self.digest:
            return SimpleNamespace(ok=False, error=SimpleNamespace(message="not found"))
        return SimpleNamespace(ok=True, data={
            "digest": digest, "filename": "markers.mp4", "media_type": "video/mp4",
            "size": self.source.stat().st_size,
        })

    def open(self, project: str, digest: str, **kwargs):
        assert project == "h3-handle-project" and digest == self.digest
        root = Path(kwargs["cache_root"])
        root.mkdir(parents=True)
        path = root / "markers.mp4"
        path.write_bytes(self.source.read_bytes())
        self.opened.append(path)
        return SimpleNamespace(ok=True, data={"local_path": str(path)})


def test_h3_digest_handle_reaches_real_build_state_and_composition(tmp_path: Path, monkeypatch) -> None:
    from astrid.core.generation import entries

    source = tmp_path / "source-markers.mp4"
    _source_markers_and_pulse(source)
    source_bytes = source.read_bytes()
    digest = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    client = SimpleNamespace(media=_ProjectMedia(digest, source))
    request_document = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": digest, "range": [0.5, 2.5]},
        "output": {"duration": 2},
        "content": {"prompt": "Keep the source outside the short generated interval."},
        "changes": {
            "video": [{"during": [0.25, 0.5], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [],
        },
        "references": [],
        "overrides": {},
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request_document), encoding="utf-8")
    generated = tmp_path / "generated-video-only.mkv"
    _generated(generated)

    from astrid.sdk import generation
    monkeypatch.setattr(generation, "_resolve_invoke_destination",
                        lambda **_kwargs: (None, "h3-handle-project"))
    admission: dict = {}
    job_dir = tmp_path / "actual-h3-job"

    def build_and_finish(self, **kwargs):
        inputs = kwargs["inputs"]
        assert set(inputs["asset_map"]) == {digest}
        materialized = Path(inputs["asset_map"][digest])
        assert materialized.is_file() and materialized.read_bytes() == source_bytes
        admission["materialized_path"] = materialized
        admission["project"] = kwargs["project"]
        admission["context"] = kwargs["idempotency_context"]

        import astrid.packs.vibecomfy.extensions  # noqa: F401
        entry = entries.find_entry("h3")
        hooks = entries.entry_hooks(entry)
        assert set(hooks) == {"build", "after_outputs"}
        job = {**entries.entry_job(entry, inputs, job_dir, hooks=hooks),
               "manifest": {"models": [], "extra": {"nodes": []}}}
        engine = entries.engine_table()["comfyui"]
        built = entries.build_entry(entry, inputs, job_dir)
        state_path = job_dir / "h3" / "build-state.json"
        assert built is not None and built["workflow"] is not None
        assert state_path.is_file()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        prepared_source = Path(state["source"])
        assert prepared_source.is_file()
        conform = state["preparation"]["source_conform"]
        assert conform["source"]["sha256"] == digest[7:]
        assert conform["source"]["range_seconds"] == ["0.5", "2.5"]
        assert built["bindings"]["source_video"]
        graph_source = Path(built["assets"]["source_video"])
        assert graph_source.is_file()
        assert hashlib.sha256(graph_source.read_bytes()).hexdigest() == conform["output"]["sha256"]
        admission["build_state"] = state

        finished = engine.finish(job, [str(generated)])
        assert finished and Path(finished[0]).is_file()
        verify_path = job_dir / "h3" / "verify.json"
        assert verify_path.is_file()
        admission["verification"] = json.loads(verify_path.read_text(encoding="utf-8"))
        admission["candidate"] = Path(finished[0])
        return finished

    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl", build_and_finish)
    request_inputs = {"request": str(request_path)}
    assert "asset_map" not in request_inputs
    result = GenerationFacade()._generate_on_target(
        model="h3", inputs=request_inputs, client=client,
        project="caller-project", out=None, project_root=None, idempotency_context={},
    )
    assert "asset_map" not in request_inputs

    assert result and Path(result[0]) == admission["candidate"]
    assert admission["project"] == "h3-handle-project"
    assert admission["context"]["media_handles"] == [
        {"port": "request.asset", "handle": digest, "digest": digest},
    ]
    assert not admission["materialized_path"].exists()  # adapter-owned temp materialization was released
    verification = admission["verification"]
    assert verification["preservation"]["status"] == "protected_sample_evidence"

    original = _gray_markers(source)
    candidate = _gray_markers(admission["candidate"])
    marker = lambda frames, index: float(frames[index, 280:296, 500:524].mean())
    source_marker_values = [round(marker(original, index)) for index in range(len(original))]
    assert len(set(source_marker_values)) == 96  # one independently decodable marker per source frame
    assert len(candidate) == 48
    for output_frame, source_frame in ((0, 12), (24, 36)):
        assert abs(marker(candidate, output_frame) - marker(original, source_frame)) <= 8
    assert marker(candidate, 8) > 225  # only [0.25, 0.5] came from the generated candidate

    # The 880Hz source pulse at original [1.5, 1.75]s becomes [1.0, 1.25]s
    # after selecting [0.5, 2.5]s; check both its new and preserved locations.
    assert abs(_pulse_peak(admission["candidate"], 1.08) - 880) <= 15
    assert abs(_pulse_peak(admission["candidate"], 0.55) - 440) <= 15
