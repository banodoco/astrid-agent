"""Ranged render audio facts use timeline time in review and inspection."""
from __future__ import annotations

import copy
import hashlib
import json

from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import (
    compact_render_receipt,
    inspect_filmstrip,
)
from astrid.packs.rendering.actions.timeline_visualize.render_clock import RenderClock


def _analysis():
    """A file-clock analysis with a quiet interval at file seconds 1–2."""
    return {
        "schema_version": 1,
        "analysis_version": "astrid.audio-analysis.v1",
        "analysis_identity": "sha256:" + "a" * 64,
        "render_digest": "sha256:" + "b" * 64,
        "status": "ok",
        "stream": {"sample_rate": 48_000, "channels": 1},
        "presentation_origin": {"seconds": [0, 1], "time_base": "1/48000"},
        "waveform": {
            "sample_count": 240_000,
            "duration": [5, 1],
            "duration_seconds": 5,
            "sample_bounds": [0, 240_000],
            "levels": [{
                "id": "level-2",
                "target_bins": 2,
                "bin_width_samples": 120_000,
                "bins": [
                    {"index": 0, "start_sample": 0, "end_sample": 120_000, "rms": [0.1], "peak": [0.2]},
                    {"index": 1, "start_sample": 120_000, "end_sample": 240_000, "rms": [0.0], "peak": [0.0]},
                ],
            }],
        },
        "quiet_gaps": [{
            "start": [1, 1], "end": [2, 1], "duration": [1, 1], "duration_seconds": 1,
            "start_sample": 48_000, "end_sample": 96_000,
            "measurement": "low_amplitude", "threshold": 0.01,
        }],
        # Frozen speech annotation times already use the timeline clock.
        "speech": {"status": "projected", "phrases": [
            {"start": [7, 1], "end": [8, 1], "text": "already on timeline clock"},
        ]},
    }


def _snapshot():
    return {
        "project_slug": "demo",
        "timeline_id": "main",
        "render_run_id": "run",
        "video_digest": "sha256:" + "b" * 64,
        "fps_rational": [24, 1],
        "duration_frames": 192,
        "metadata": {"timeline_range": {
            "start_frame": 144, "end_frame": 336, "fps_rational": [24, 1],
        }},
    }


def _inspect_with_audio(tmp_path, snapshot, audio, *, range_value):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "frames").mkdir(exist_ok=True)
    (tmp_path / "frames/frame.jpg").write_bytes(b"frame")
    (tmp_path / "audio-analysis.json").write_text(json.dumps(audio), encoding="utf-8")
    index = compact_render_receipt({"cards": [], "sampling": {}, "coverage": {}}, snapshot, tmp_path)
    (tmp_path / "frame-index.json").write_text(json.dumps(index), encoding="utf-8")
    outputs = []
    for rel in ("frame-index.json", "audio-analysis.json", "frames/frame.jpg"):
        path = tmp_path / rel
        outputs.append({"path": rel, "bytes": path.stat().st_size,
                        "content_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"kind": "timeline_filmstrip", "outputs": outputs}), encoding="utf-8")
    return inspect_filmstrip(manifest, section="audio", range_value=range_value)


def test_ranged_audio_projection_agrees_with_contact_review_and_inspection(tmp_path, monkeypatch):
    snapshot = _snapshot()
    clock = RenderClock(snapshot)
    raw = _analysis()
    raw_before = copy.deepcopy(raw)

    # The projection API belongs with RenderClock so the recorded integer frame
    # boundary, rather than the originally requested seconds, owns the offset.
    projected = clock.project_audio_analysis(raw)

    assert raw == raw_before  # the content-addressed file-clock cache is reusable
    assert projected["quiet_gaps"][0]["start"] == [7, 1]
    assert projected["quiet_gaps"][0]["end"] == [8, 1]
    assert projected["quiet_gaps"][0]["duration"] == [1, 1]
    assert projected["quiet_gaps"][0]["start_sample"] == 48_000
    assert projected["quiet_gaps"][0]["end_sample"] == 96_000
    assert projected["presentation_origin"]["seconds"] == [6, 1]
    assert projected["waveform"]["duration"] == [5, 1]
    assert projected["waveform"]["sample_bounds"] == [0, 240_000]
    assert projected["waveform"]["levels"][0]["bins"][1]["start_sample"] == 120_000
    assert projected["speech"] == raw["speech"]  # speech is frozen in timeline time already

    in_window = _inspect_with_audio(tmp_path / "inside", snapshot, projected, range_value="7..8")
    assert in_window["ok"], in_window
    records = in_window["data"]["records"]
    assert any(row.get("kind") == "low_energy" and row["start_seconds"] == 7 and row["end_seconds"] == 8
               for row in records)
    assert any(row.get("kind") == "waveform_bin" and row["start_seconds"] == 6 and row["end_seconds"] == 8.5
               for row in records)

    old_file_window = _inspect_with_audio(tmp_path / "outside", snapshot, projected, range_value="1..2")
    assert old_file_window["ok"], old_file_window
    assert not any(row.get("kind") in {"low_energy", "waveform_bin"}
                   for row in old_file_window["data"]["records"])

    # Contact render review already applies the same RenderClock offset to
    # silence intervals; confirm its interval matches the projected SDK fact.
    from astrid.packs.rendering.actions.timeline_visualize import render_review

    monkeypatch_review = {"integrated_lufs": -20.0, "lra_lu": 1.0, "true_peak_dbtp": -2.0,
                          "silences": [{"start": 1.0, "end": 2.0, "duration": 1.0}]}
    monkeypatch.setattr(render_review, "probe", lambda *_args: (5.0, True))
    monkeypatch.setattr(render_review, "sample_times", lambda *_args: [1.0])
    monkeypatch.setattr(render_review, "sample_frames", lambda *_args: [])
    monkeypatch.setattr(render_review, "compose_contact", lambda *_args, **_kwargs: tmp_path / "contact.png")
    monkeypatch.setattr(render_review, "measure_audio", lambda *_args: copy.deepcopy(monkeypatch_review))
    contact = render_review.review_render(tmp_path / "render.mp4", tmp_path / "contact-review", clock=clock)
    assert contact["silences"] == [{"start": 7.0, "end": 8.0, "duration": 1.0}]
    assert projected["quiet_gaps"][0]["start"] == [int(contact["silences"][0]["start"]), 1]
    assert projected["quiet_gaps"][0]["end"] == [int(contact["silences"][0]["end"]), 1]
