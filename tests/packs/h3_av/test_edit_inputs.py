from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from astrid.packs.h3_av.shared import prepare
from astrid.packs.h3_av.shared.generation import LIMITS
from astrid.packs.h3_av.shared.request import H3Request
from astrid.packs.h3_av.shared.request_v2 import H3RequestError, _validate_character_swap_media, normalize_v2


def test_character_swap_minimum_and_additional_references_are_admitted():
    _validate_character_swap_media({"adapter": {"id": "character_swap_v1"}}, [
        {"role": "reference", "modality": "image"},
        {"role": "reference", "modality": "video"},
        {"role": "reference", "modality": "image"},
    ], False)
    with pytest.raises(H3RequestError, match="at least one video"):
        _validate_character_swap_media({"adapter": {"id": "character_swap_v1"}}, [
            {"role": "reference", "modality": "image"},
        ], False)


def test_shared_limits_table_has_node_and_lowering_sources():
    assert LIMITS["builder_slots"] == {"value": {"image": 9, "video": 3, "audio": 3}, "source": "node"}
    assert LIMITS["continuation_references"]["source"] == "Astrid lowering"
    assert LIMITS["conditioning_files"]["value"] == 12
    # Builder validation is pinned to the cached MiniMaxH3ReferenceToVideo schema.
    from astrid.packs.h3_av.shared import generation

    assert generation.IMAGE_REFERENCE_CAPACITY == LIMITS["builder_slots"]["value"]["image"]
    assert generation.VIDEO_REFERENCE_CAPACITY == LIMITS["builder_slots"]["value"]["video"]


def _request(items):
    return H3Request({"version": 2, "media": items, "continuation": False, "duration": 6}, "test")


def test_preclaim_measures_each_occurrence_and_sums_video_soundtrack(monkeypatch, tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.touch()
    image = tmp_path / "image.png"
    from PIL import Image
    Image.new("RGB", (8, 8)).save(image)
    items = [
        {"occurrence_id": "v1", "asset": "v", "role": "reference", "modality": "video", "audio": True},
        {"occurrence_id": "v2", "asset": "v", "role": "reference", "modality": "video", "audio": False,
         "resolved_range": [0, 240]},
        {"occurrence_id": "i1", "asset": "i", "role": "reference", "modality": "image"},
    ]
    probes = iter([
        {"streams": [{"codec_type": "video", "duration": "5", "nb_frames": "120", "avg_frame_rate": "24/1", "width": 64, "height": 32},
                      {"codec_type": "audio", "duration": "5", "sample_rate": "48000", "channels": 2}], "format": {"duration": "5"}},
        {"streams": [{"codec_type": "video", "duration": "12", "nb_frames": "288", "avg_frame_rate": "24/1", "width": 64, "height": 32}], "format": {"duration": "12"}},
        {"streams": [{"codec_type": "video", "width": 8, "height": 8}], "format": {}},
    ])
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda path: next(probes))
    records = [
        {"asset": "v", "status": "resolved", "path": str(video)},
        {"asset": "i", "status": "resolved", "path": str(image)},
    ]
    measured = prepare._admit_conditioning(_request(items), records)
    assert [row["retained_picture_frames"] for row in measured[:2]] == [None, None]
    assert measured[0]["audio_tag"] == "<Audio 1>"
    assert measured[0]["slot"] == "ref_videos.ref_video_0"
    assert measured[0]["tag"] == "<Video 1>"
    assert measured[2]["tag"] == "<Picture 1>"
    assert measured[0]["audio_duration_seconds"] == 5
    assert measured[0]["effective_size"] == {"width": 64, "height": 32}
    assert measured[1]["resource_estimate"]["raw_bytes"] is None
    assert measured[1]["resource_estimate"]["qualified"] is False
    assert measured[2]["effective_size"] == {"width": 32, "height": 32}
    assert measured[2]["resource_estimate"]["raw_bytes"] == 32 * 32 * 3
    assert measured[2]["slot"] == "ref_images.ref_image_0"


def test_unresolved_occurrences_still_count_against_slots_and_file_limit():
    items = [
        {"occurrence_id": f"i{index}", "asset": f"asset-{index}", "role": "reference", "modality": "image"}
        for index in range(10)
    ]
    with pytest.raises(prepare.PreparationError, match="builder slots"):
        prepare._admit_conditioning(_request(items), [])
    at_limit = [
        {"occurrence_id": f"i{index}", "asset": f"asset-{index}", "role": "reference", "modality": "image"}
        for index in range(9)
    ]
    assert len(prepare._admit_conditioning(_request(at_limit), [])) == 9


def test_replacement_audio_reference_is_excluded_from_conditioning_admission():
    item = {"occurrence_id": "replacement", "asset": "audio", "role": "reference", "modality": "audio", "use": "replacement"}
    assert prepare._admit_conditioning(_request([item]), []) == []
    items = [
        *[{"occurrence_id": f"i{index}", "asset": f"i{index}", "role": "reference", "modality": "image"} for index in range(9)],
        *[{"occurrence_id": f"v{index}", "asset": f"v{index}", "role": "reference", "modality": "video"} for index in range(3)],
        {"occurrence_id": "a0", "asset": "a0", "role": "reference", "modality": "audio"},
    ]
    with pytest.raises(prepare.PreparationError, match="conditioning files 12"):
        prepare._admit_conditioning(_request(items), [])


@pytest.mark.parametrize("dimension, accepted", [(32, True), (16384, True), (31, False), (16385, False), (34, False)])
def test_canvas_dimension_limits_are_enforced_at_prepare_boundary(monkeypatch, dimension: int, accepted: bool):
    request = _request([])
    monkeypatch.setattr(prepare, "_resolve_assets", lambda *_: [])
    monkeypatch.setattr(prepare, "_admit_conditioning", lambda *_: [])
    monkeypatch.setattr(prepare, "_validate_mask_assets", lambda *_: None)
    monkeypatch.setattr(prepare, "_check_source_av_rates", lambda *_: None)
    monkeypatch.setattr(prepare, "_v2_mask_schedule", lambda *_args, **_kwargs: {"status": "ready"})
    if accepted:
        assert prepare.prepare_request(request, width=dimension, height=32)["status"] == "prepared"
    else:
        with pytest.raises(prepare.PreparationError, match="width and height 32–16384, multiples of 32"):
            prepare.prepare_request(request, width=dimension, height=32)


@pytest.mark.parametrize("seconds", [1.99, 15.01])
def test_preclaim_rejects_clip_duration_boundaries(monkeypatch, tmp_path: Path, seconds: float):
    media = tmp_path / "audio.wav"
    media.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda path: {
        "streams": [{"codec_type": "audio", "duration": str(seconds)}], "format": {"duration": str(seconds)},
    })
    item = {"occurrence_id": "a", "asset": "a", "role": "reference", "modality": "audio"}
    records = [{"asset": "a", "status": "resolved", "path": str(media)}]
    with pytest.raises(prepare.PreparationError, match="conditioning clip 2–15 seconds"):
        prepare._admit_conditioning(_request([item]), records)


@pytest.mark.parametrize("seconds", [2, 15])
def test_preclaim_accepts_clip_duration_endpoints(monkeypatch, tmp_path: Path, seconds: float):
    media = tmp_path / "audio.wav"
    media.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda path: {
        "streams": [{"codec_type": "audio", "duration": str(seconds), "sample_rate": "48000", "channels": 1}],
        "format": {"duration": str(seconds)},
    })
    item = {"occurrence_id": "a", "asset": "a", "role": "reference", "modality": "audio"}
    records = [{"asset": "a", "status": "resolved", "path": str(media)}]
    assert prepare._admit_conditioning(_request([item]), records)[0]["audio_duration_seconds"] == seconds


def test_preclaim_rejects_trim_past_measured_stream(monkeypatch, tmp_path: Path):
    media = tmp_path / "audio.wav"
    media.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda path: {
        "streams": [{"codec_type": "audio", "duration": "5"}], "format": {"duration": "5"},
    })
    item = {"occurrence_id": "a", "asset": "a", "role": "reference", "modality": "audio", "resolved_range": [0, 6 * 48000]}
    records = [{"asset": "a", "status": "resolved", "path": str(media)}]
    with pytest.raises(prepare.PreparationError, match="trim ends past measured stream duration"):
        prepare._admit_conditioning(_request([item]), records)


def test_public_preparation_rejects_real_over_limit_audio_before_claim(tmp_path: Path):
    media = tmp_path / "long.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=48000", "-t", "16", str(media)],
                   check=True, capture_output=True)
    request = normalize_v2({"version": 2, "prompt": "A scene", "duration": 6,
        "media": [{"asset": "audio-ref", "role": "reference", "modality": "audio"}]})
    with pytest.raises(prepare.PreparationError, match="conditioning clip 2–15 seconds"):
        prepare.prepare_request(request, asset_map={"audio-ref": str(media)})


def test_public_preparation_reports_valid_standalone_audio_binding(tmp_path: Path):
    media = tmp_path / "voice.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", str(media)],
                   check=True, capture_output=True)
    request = normalize_v2({"version": 2, "prompt": "A scene", "duration": 6,
        "media": [{"asset": "voice", "role": "reference", "modality": "audio"}]})
    prepared = prepare.prepare_request(request, asset_map={"voice": str(media)})
    row = prepared["conditioning_measurements"][0]
    assert row["slot"] == "ref_audios.ref_audio_0"
    assert row["tag"] == "<Audio 1>"
    assert row["input_type"] == "AUDIO"
    assert row["audio_duration_seconds"] == pytest.approx(2, abs=0.01)
    assert row["resource_estimate"]["qualified"] is True
    assert prepared["resource_estimate"]["reference_rows"] == 1


def test_public_preparation_rejects_unreadable_reference_metadata(tmp_path: Path):
    media = tmp_path / "broken.wav"
    media.write_bytes(b"not an audio stream")
    request = normalize_v2({"version": 2, "prompt": "A scene", "duration": 6,
        "media": [{"asset": "broken", "role": "reference", "modality": "audio"}]})
    with pytest.raises(prepare.PreparationError, match="conditioning media .* is unreadable"):
        prepare.prepare_request(request, asset_map={"broken": str(media)})


def test_individually_valid_clips_cannot_exceed_modality_total(monkeypatch, tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda path: {
        "streams": [{"codec_type": "video", "duration": "6"}], "format": {"duration": "6"},
    })
    items = [{"occurrence_id": f"v{index}", "asset": "v", "role": "reference", "modality": "video", "audio": False}
             for index in range(3)]
    records = [{"asset": "v", "status": "resolved", "path": str(video)}]
    with pytest.raises(prepare.PreparationError, match="per modality total 15 seconds: video"):
        prepare._admit_conditioning(_request(items), records)


def test_soundtracks_and_standalone_audio_share_modality_total(monkeypatch, tmp_path: Path):
    video = tmp_path / "clip.mp4"
    audio = tmp_path / "audio.wav"
    video.touch()
    audio.touch()
    video_probe = {"streams": [{"codec_type": "video", "duration": "5"}, {"codec_type": "audio", "duration": "5"}],
                   "format": {"duration": "5"}}
    audio_probe = {"streams": [{"codec_type": "audio", "duration": "2"}], "format": {"duration": "2"}}
    probes = iter([video_probe, video_probe, video_probe, audio_probe])
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _path: next(probes))
    items = [
        *[{"occurrence_id": f"v{index}", "asset": "v", "role": "reference", "modality": "video", "audio": True} for index in range(3)],
        {"occurrence_id": "a", "asset": "a", "role": "reference", "modality": "audio"},
    ]
    records = [{"asset": "v", "status": "resolved", "path": str(video)}, {"asset": "a", "status": "resolved", "path": str(audio)}]
    with pytest.raises(prepare.PreparationError, match="per modality total 15 seconds: audio"):
        prepare._admit_conditioning(_request(items), records)


@pytest.mark.parametrize("streams, message", [
    ([{"codec_type": "video", "duration": "5"}], "requests audio but the video has no audio stream"),
    ([{"codec_type": "video", "duration": "5"}, {"codec_type": "audio", "duration": "4"}], "trim ends past measured audio stream duration"),
])
def test_enabled_video_soundtrack_must_exist_and_cover_trim(monkeypatch, tmp_path: Path, streams, message):
    media = tmp_path / "video.mp4"
    media.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda path: {"streams": streams, "format": {"duration": "5"}})
    item = {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": True,
            "resolved_range": [0, 5 * 24]}
    records = [{"asset": "v", "status": "resolved", "path": str(media)}]
    with pytest.raises(prepare.PreparationError, match=message):
        prepare._admit_conditioning(_request([item]), records)


def test_audio_tags_follow_graph_order_even_when_authored_first():
    items = [
        {"occurrence_id": "a", "asset": "a", "role": "reference", "modality": "audio"},
        {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": True},
    ]
    rows = prepare._admit_conditioning(_request(items), [])
    assert rows[0]["tag"] == "<Audio 2>"
    assert rows[1]["audio_tag"] == "<Audio 1>"


def test_untrimmed_soundtrack_uses_video_span_and_requires_stream_coverage(monkeypatch, tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    item = {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": True}
    records = [{"asset": "v", "status": "resolved", "path": str(path)}]
    probe = {"streams": [{"codec_type": "video", "duration": "5"}, {"codec_type": "audio", "duration": "10"}], "format": {"duration": "10"}}
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _: probe)
    assert prepare._admit_conditioning(_request([item]), records)[0]["audio_duration_seconds"] == 10
    probe["streams"][1]["duration"] = "4"
    with pytest.raises(prepare.PreparationError, match="past measured audio stream duration"):
        prepare._admit_conditioning(_request([item]), records)
    del probe["streams"][1]["duration"]
    with pytest.raises(prepare.PreparationError, match="cannot measure soundtrack"):
        prepare._admit_conditioning(_request([item]), records)


def test_real_video_rejects_shorter_soundtrack(tmp_path):
    path = tmp_path / "short-audio.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=size=32x32:rate=24:duration=5",
                    "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=3",
                    "-c:v", "mpeg4", "-c:a", "aac", str(path)], check=True)
    item = {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": True}
    records = [{"asset": "v", "status": "resolved", "path": str(path)}]
    with pytest.raises(prepare.PreparationError, match="past measured audio stream duration"):
        prepare._admit_conditioning(_request([item]), records)


@pytest.mark.parametrize("modality", ["video", "audio"])
@pytest.mark.parametrize("count, accepted", [(3, True), (4, False)])
def test_reference_slot_boundaries(modality, count, accepted):
    items = [{"occurrence_id": str(i), "asset": str(i), "role": "reference", "modality": modality} for i in range(count)]
    if accepted:
        assert len(prepare._admit_conditioning(_request(items), [])) == count
    else:
        with pytest.raises(prepare.PreparationError, match="builder slots"):
            prepare._admit_conditioning(_request(items), [])


@pytest.mark.parametrize("extra, accepted", [(0, True), (1, False)])
def test_conditioning_file_boundary(extra, accepted):
    items = [{"occurrence_id": f"{kind}{i}", "asset": f"{kind}{i}", "role": "reference", "modality": kind}
             for kind, count in [("image", 9), ("video", 3), ("audio", extra)] for i in range(count)]
    if accepted:
        assert len(prepare._admit_conditioning(_request(items), [])) == 12
    else:
        with pytest.raises(prepare.PreparationError, match="conditioning files 12"):
            prepare._admit_conditioning(_request(items), [])


@pytest.mark.parametrize("modality, count, accepted", [("image", 2, True), ("image", 3, False), ("video", 0, True), ("video", 1, False), ("audio", 0, True), ("audio", 1, False)])
def test_continuation_reference_boundaries(modality, count, accepted):
    items = [{"occurrence_id": str(i), "asset": str(i), "role": "reference", "modality": modality} for i in range(count)]
    req = H3Request({"version": 2, "media": items, "continuation": True}, "test")
    if accepted:
        assert len(prepare._admit_conditioning(req, [])) == count
    else:
        with pytest.raises(prepare.PreparationError, match="continuation references"):
            prepare._admit_conditioning(req, [])


@pytest.mark.parametrize("frames, warned", [(123, True), (124, False), (362, False), (363, True)])
def test_preparation_reports_duration_warning(monkeypatch, frames, warned):
    req = H3Request({"version": 2, "media": [], "continuation": False, "duration": frames / 24}, "test")
    monkeypatch.setattr(prepare, "_resolve_assets", lambda *_: [])
    monkeypatch.setattr(prepare, "_validate_mask_assets", lambda *_: None)
    monkeypatch.setattr(prepare, "_check_source_av_rates", lambda *_: None)
    monkeypatch.setattr(prepare, "_v2_mask_schedule", lambda *_args, **_kwargs: {"status": "ready"})
    warnings = prepare.prepare_request(req)["warnings"]
    duration_warnings = [row for row in warnings if row["limit"] == "job_duration_seconds"]
    assert bool(duration_warnings) is warned
    assert any(row["limit"] == "text_only_documented_use" and "outside the documented use" in row["message"] for row in warnings)


def test_character_swap_requires_each_minimum_reference():
    settings = {"adapter": {"id": "character_swap_v1"}}
    for missing in ("image", "video"):
        refs = [{"role": "reference", "modality": kind} for kind in ("image", "video") if kind != missing]
        with pytest.raises(H3RequestError, match="references"):
            _validate_character_swap_media(settings, refs, False)
    _validate_character_swap_media(settings, [{"role": "reference", "modality": kind} for kind in ("image", "video")], False)


@pytest.mark.parametrize("modality", ["video", "audio"])
@pytest.mark.parametrize("seconds, accepted", [(15, True), (15.01, False)])
def test_modality_total_boundary(monkeypatch, tmp_path, modality, seconds, accepted):
    path = tmp_path / "clip"
    path.touch()
    if modality == "video" and not accepted:
        seconds = 15.1  # More than one 24-fps frame beyond the limit.
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _: {"streams": [{"codec_type": modality, "duration": str(seconds / 2)}]})
    items = [{"occurrence_id": str(i), "asset": "x", "role": "reference", "modality": modality, "audio": False} for i in range(2)]
    records = [{"asset": "x", "status": "resolved", "path": str(path)}]
    if accepted:
        assert len(prepare._admit_conditioning(_request(items), records)) == 2
    else:
        with pytest.raises(prepare.PreparationError, match="per modality total 15 seconds"):
            prepare._admit_conditioning(_request(items), records)


def test_real_reference_frames_follow_loader_and_node_truncation(tmp_path):
    path = tmp_path / "thirty-fps.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=size=96x64:rate=30:duration=3", "-c:v", "mpeg4", str(path)], check=True)
    item = {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": False,
            "resolved_range": [0, 48]}
    records = [{"asset": "v", "status": "resolved", "path": str(path)}]
    row = prepare._admit_conditioning(_request([item]), records)[0]
    assert row["retained_picture_frames_estimate"] == {"value": 39, "kind": "metadata_upper_bound", "observed": False}
    assert row["retained_picture_frames"] is None
    assert row["effective_size"] == {"width": 96, "height": 64}
    assert row["resource_estimate"]["raw_bytes"] is None
    assert row["resource_estimate"]["raw_bytes_upper_bound"] == 39 * 96 * 64 * 3
    assert row["resource_estimate"]["qualified"] is False


def test_reference_size_matches_pinned_node_resize_rules():
    assert prepare._reference_size("image", 2048, 2048) == {"width": 768, "height": 768}
    assert prepare._reference_size("video", 3840, 2160) == {"width": 1344, "height": 768}
    assert prepare._reference_size("video", 320, 200) == {"width": 320, "height": 192}


def test_unmeasurable_video_frame_count_is_not_reported_as_retained(monkeypatch, tmp_path):
    path = tmp_path / "v"
    path.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _: {"streams": [{"codec_type": "video", "duration": "3", "width": 64, "height": 64}]})
    item = {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": False}
    row = prepare._admit_conditioning(_request([item]), [{"asset": "v", "status": "resolved", "path": str(path)}])[0]
    assert row["retained_picture_frames"] is None
    assert row["resource_estimate"]["qualified"] is False


@pytest.mark.parametrize("avg_rate, nominal_rate", [("24/1", "24/1"), ("24000/1001", "30/1")])
def test_video_metadata_does_not_claim_resampled_count_or_decode(monkeypatch, tmp_path, avg_rate, nominal_rate):
    path = tmp_path / "video.mp4"
    path.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _: {"streams": [{"codec_type": "video", "duration": "3", "nb_frames": "72", "avg_frame_rate": avg_rate, "r_frame_rate": nominal_rate, "width": 96, "height": 64}]})
    monkeypatch.setattr(prepare.subprocess, "run", lambda *args, **kwargs: pytest.fail("preparation must not decode video"))
    item = {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": False, "resolved_range": [0, 48]}
    row = prepare._admit_conditioning(_request([item]), [{"asset": "v", "status": "resolved", "path": str(path)}])[0]
    assert row["retained_picture_frames"] is None
    assert row["resource_estimate"]["raw_bytes"] is None
    assert not row["resource_estimate"]["qualified"]


def test_video_duration_near_limit_warns_and_clear_excess_fails(monkeypatch, tmp_path):
    path = tmp_path / "clip"
    path.touch()
    duration = [15.01]
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _: {"streams": [{"codec_type": "video", "duration": str(duration[0]), "width": 96, "height": 64}]})
    item = {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": False}
    records = [{"asset": "v", "status": "resolved", "path": str(path)}]
    warnings = []
    row = prepare._admit_conditioning(_request([item]), records, warnings)[0]
    assert {warning["limit"] for warning in warnings} == {"clip_seconds", "modality_seconds"}
    assert row["retained_picture_frames"] is None
    duration[0] = 15.1
    with pytest.raises(prepare.PreparationError, match="conditioning clip"):
        prepare._admit_conditioning(_request([item]), records, [])


def test_file_limit_warns_at_twelve():
    items = [{"occurrence_id": f"{kind}{i}", "asset": f"{kind}{i}", "role": "reference", "modality": kind} for kind, count in [("image", 9), ("video", 3)] for i in range(count)]
    warnings = []
    prepare._admit_conditioning(_request(items), [], warnings)
    assert any(warning["limit"] == "conditioning_files" for warning in warnings)


def test_uncapped_video_estimate_includes_timestamp_boundary_frame(monkeypatch, tmp_path):
    path = tmp_path / "v"
    path.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _: {"streams": [{"codec_type": "video", "duration": "2.2916666666666665", "width": 96, "height": 64}]})
    item = {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": False}
    row = prepare._admit_conditioning(_request([item]), [{"asset": "v", "status": "resolved", "path": str(path)}])[0]
    # 55 metadata frames align to 39; one boundary frame aligns to 56.
    assert row["retained_picture_frames_estimate"]["value"] == 56
    assert row["retained_picture_frames"] is None


def test_exact_trim_ticks_refuse_short_clip_and_excess_total(monkeypatch, tmp_path):
    path = tmp_path / "clip"
    path.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _: {"streams": [{"codec_type": "video", "duration": "10"}]})
    records = [{"asset": "v", "status": "resolved", "path": str(path)}]
    def item(i, end):
        return {"occurrence_id": str(i), "asset": "v", "role": "reference", "modality": "video", "audio": False, "resolved_range": [0, end]}
    with pytest.raises(prepare.PreparationError, match="conditioning clip"):
        prepare._admit_conditioning(_request([item(0, 47)]), records)
    with pytest.raises(prepare.PreparationError, match="per modality total"):
        prepare._admit_conditioning(_request([item(i, 122) for i in range(3)]), records)


@pytest.mark.parametrize("frames, accepted", [(3592, True), (3593, False), (3600, False)])
def test_preparation_rejects_lengths_outside_node_grid(monkeypatch, frames, accepted):
    req = H3Request({"version": 2, "media": [], "continuation": False, "duration": frames / 24}, "test")
    monkeypatch.setattr(prepare, "_resolve_assets", lambda *_: [])
    monkeypatch.setattr(prepare, "_validate_mask_assets", lambda *_: None)
    monkeypatch.setattr(prepare, "_check_source_av_rates", lambda *_: None)
    monkeypatch.setattr(prepare, "_v2_mask_schedule", lambda *_args, **_kwargs: {"status": "ready"})
    if accepted:
        assert prepare.prepare_request(req)["resource_estimate"]["target_rows"] == 3592
    else:
        with pytest.raises(prepare.PreparationError, match="job length frames"):
            prepare.prepare_request(req)


def test_masked_reference_bindings_are_provisional_and_audio_total_unknown(monkeypatch, tmp_path):
    from PIL import Image
    image = tmp_path / "image.png"
    Image.new("RGB", (32, 32)).save(image)
    video = tmp_path / "reference.mp4"
    video.touch()
    items = [
        {"occurrence_id": "a", "asset": "a", "role": "reference", "modality": "image"},
        {"occurrence_id": "b", "asset": "b", "role": "reference", "modality": "image"},
        {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": True},
        {"occurrence_id": "source", "asset": "s", "role": "timeline", "modality": "video", "edit": [{"guides": ["b"]}]},
    ]
    records = [{"asset": name, "status": "resolved", "path": str(path)} for name, path in [("a", image), ("b", image), ("v", video), ("s", video)]]
    monkeypatch.setattr(prepare, "_resolve_assets", lambda *_: records)
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda path: {"streams": [{"codec_type": "video", "width": 32, "height": 32}] if path == image else [{"codec_type": "video", "duration": "3"}, {"codec_type": "audio", "duration": "3"}]})
    monkeypatch.setattr(prepare, "_validate_mask_assets", lambda *_: None)
    monkeypatch.setattr(prepare, "_check_source_av_rates", lambda *_: None)
    monkeypatch.setattr(prepare, "_v2_mask_schedule", lambda *_args, **_kwargs: {"status": "ready"})
    result = prepare.prepare_request(H3Request({"version": 2, "media": items, "continuation": False, "duration": 6}, "test"))
    assert result["conditioning_measurements"][1]["tag"] is None
    assert result["conditioning_measurements"][1]["slot"] is None
    assert result["conditioning_measurements"][1]["binding_status"] == "provisional_until_lowering"
    assert result["resource_estimate"]["reference_raw_audio_bytes"] is None


@pytest.mark.parametrize("start, end, accepted", [(1, 49, True), (1, 48, False), (25, 385, True), (25, 386, False)])
def test_exact_trim_limits_with_nonzero_start(monkeypatch, tmp_path, start, end, accepted):
    path = tmp_path / "clip"
    path.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _: {"streams": [{"codec_type": "video", "duration": "20"}]})
    item = {"occurrence_id": "v", "asset": "v", "role": "reference", "modality": "video", "audio": False, "resolved_range": [start, end]}
    records = [{"asset": "v", "status": "resolved", "path": str(path)}]
    if accepted:
        assert len(prepare._admit_conditioning(_request([item]), records)) == 1
    else:
        with pytest.raises(prepare.PreparationError, match="conditioning clip"):
            prepare._admit_conditioning(_request([item]), records)


def test_exact_trim_totals_with_nonzero_starts(monkeypatch, tmp_path):
    path = tmp_path / "clip"
    path.touch()
    monkeypatch.setattr(prepare, "_probe_conditioning", lambda _: {"streams": [{"codec_type": "video", "duration": "20"}]})
    records = [{"asset": "v", "status": "resolved", "path": str(path)}]
    def items(end):
        return [{"occurrence_id": str(i), "asset": "v", "role": "reference", "modality": "video", "audio": False, "resolved_range": [1, end]} for i in range(3)]
    assert len(prepare._admit_conditioning(_request(items(121)), records)) == 3
    with pytest.raises(prepare.PreparationError, match="per modality total"):
        prepare._admit_conditioning(_request(items(122)), records)


def test_continuation_resource_estimate_uses_actual_raw_extension(monkeypatch):
    from fractions import Fraction
    from astrid.packs.h3_av.shared.timing import plan_continuation
    timeline = {"occurrence_id": "source", "asset": "clip", "role": "timeline", "modality": "video", "resolved_range": [0, 120]}
    request = H3Request({"version": 2, "media": [timeline], "continuation": True, "duration": 8}, "test")
    monkeypatch.setattr(prepare, "_resolve_assets", lambda *_: [])
    monkeypatch.setattr(prepare, "_validate_mask_assets", lambda *_: None)
    monkeypatch.setattr(prepare, "_check_source_av_rates", lambda *_: None)
    monkeypatch.setattr(prepare, "_v2_mask_schedule", lambda *_args, **_kwargs: {"status": "ready"})
    result = prepare.prepare_request(request)
    expected = plan_continuation(source_end=Fraction(5), output_duration=8)
    assert result["resource_estimate"]["target_rows"] == expected.raw_extension_frames


def test_unresolved_modalities_cannot_bypass_reference_limits():
    items = [{"occurrence_id": f"unknown-{index}", "asset": f"asset-{index}", "role": "reference"} for index in range(13)]
    with pytest.raises(prepare.PreparationError, match=r"unknown-0.*unresolved modality"):
        prepare._admit_conditioning(_request(items), [])

@pytest.mark.parametrize("strength", [0, 0.5, 1])
def test_audio_grant_strength_is_admitted_and_keeps_sample_boundaries(strength):
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.spec import canonical_spec, sampling_request_document
    raw = {"version": 2, "duration": 5, "prompt": "Edit", "media": [
        {"asset": "source", "role": "timeline", "modality": "video", "at": {"frame": 0},
         "range": [0, 5], "edit": [{"stream": "audio", "during": ["1/48000", 2], "strength": strength}]},
    ]}
    request = normalize_request(raw)
    op = canonical_spec(request).sampling_passes()[0].operation
    assert op.strength == strength
    assert op.during[0] * 48000 == 1
    doc = sampling_request_document(request, 0)
    assert normalize_request(doc).value["media"][0]["edit"][0]["resolved"]["range"] == [1, 96000]


@pytest.mark.parametrize("strength", [-0.1, 1.1])
def test_audio_grant_strength_rejects_values_outside_limits(strength):
    from astrid.packs.h3_av.shared.request import normalize_request
    raw = {"version": 2, "duration": 5, "prompt": "Edit", "media": [
        {"asset": "source", "role": "timeline", "modality": "video", "at": {"frame": 0},
         "range": [0, 5], "edit": [{"stream": "audio", "during": [0, 2], "strength": strength}]},
    ]}
    with pytest.raises(H3RequestError):
        normalize_request(raw)


def test_v2_mixed_audio_roundtrip_keeps_each_occurrence_and_use():
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.spec import canonical_spec, sampling_request, sampling_request_document
    raw = {"version": 2, "duration": 5, "prompt": "Edit", "media": [
        {"id": "source", "asset": "scene", "role": "timeline", "modality": "video", "at": {"frame": 0},
         "range": [0, 5], "edit": [
             {"stream": "video", "during": [0, 5], "guides": ["speaker"]},
             {"stream": "audio", "during": [0, 2], "guides": ["fixed"]},
             {"stream": "audio", "during": [2, 4]},
         ]},
        {"id": "fixed", "asset": "voice", "role": "reference", "modality": "audio", "range": [0, 2]},
        {"id": "speaker", "asset": "voice", "role": "reference", "modality": "audio", "range": [2, 4]},
    ]}
    request = normalize_request(raw)
    spec = canonical_spec(request)
    assert [ref.use for ref in spec.refs] == ["replacement", "conditioning"]
    assert [ref.range for ref in spec.refs] == [(0, 2), (2, 4)]
    assert [p.operation.stream for p in spec.sampling_passes()] == ["video", "audio", "audio"]
    # Each pass retains occurrence identity, including references not selected by it.
    for index in range(3):
        rebuilt = sampling_request(request, index)
        assert [(r["occurrence_id"], r["asset"], r.get("range"), r.get("use")) for r in rebuilt.value["media"][1:]] == [
            (r["occurrence_id"], r["asset"], r.get("range"), r.get("use")) for r in request.value["media"][1:]]


def test_derived_pass_view_refuses_loss_and_public_use_is_not_authoritative():
    import json
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.spec import sampling_request_document
    raw = {"version": 2, "duration": 5, "prompt": "Edit", "media": [
        {"id": "source", "asset": "scene", "role": "timeline", "modality": "video", "at": {"frame": 0},
         "range": [0, 5], "edit": [
             {"stream": "video", "during": [0, 5], "guides": ["speaker"]},
             {"stream": "audio", "during": [0, 2], "guides": ["fixed"]},
         ]},
        {"id": "fixed", "asset": "voice", "role": "reference", "modality": "audio", "range": [0, 2]},
        {"id": "speaker", "asset": "voice", "role": "reference", "modality": "audio", "range": [2, 4]},
    ]}
    request = normalize_request(raw)
    view = sampling_request_document(request, 0)
    with pytest.raises(H3RequestError, match="derived pass view cannot be executed"):
        normalize_request(json.loads(json.dumps(view)))
    safe_view = sampling_request_document(request, 1)
    assert normalize_request(safe_view).value["media"][1]["use"] == "replacement"
    raw["media"][1]["use"] = "replacement"
    with pytest.raises(H3RequestError):
        normalize_request(raw)


def test_planner_persists_and_reloads_classified_uses_for_each_pass(tmp_path):
    import json
    from astrid.packs.h3_av.shared.planner import plan_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.request_v2 import read_prepared_request

    source, voice = tmp_path / "source.mkv", tmp_path / "voice.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=1024x576:rate=24:duration=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5",
                    "-c:v", "libx264", "-c:a", "aac", str(source)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=660:sample_rate=48000:duration=4", "-c:a", "pcm_s16le", str(voice)], check=True)
    raw = {"version": 2, "duration": 5, "prompt": "Edit", "media": [
        {"id": "source", "asset": "scene", "role": "timeline", "modality": "video", "at": {"frame": 0},
         "range": [0, 5], "edit": [
             {"stream": "video", "during": [0, 5], "guides": ["speaker"]},
             {"stream": "audio", "during": [0, 2], "guides": ["fixed"]},
             {"stream": "audio", "during": [2, 4]},
         ]},
        {"id": "fixed", "asset": "voice", "role": "reference", "modality": "audio", "range": [0, 2]},
        {"id": "speaker", "asset": "voice", "role": "reference", "modality": "audio", "range": [2, 4]},
    ]}
    result = plan_request(normalize_request(raw), asset_map={"scene": str(source), "voice": str(voice)}, out_dir=tmp_path / "plan")
    assert result["pass_count"] == 3
    for index in range(1, 4):
        preparation = json.loads((tmp_path / "plan" / "passes" / f"{index:02d}" / "preparation.json").read_text())
        loaded = read_prepared_request(preparation["request"], preparation["request_digest"], require_normalized_v2=True)
        assert [row.get("use") for row in loaded.value["media"][1:]] == ["replacement", "conditioning"]


def test_internal_pass_refuses_a_selected_conflicting_classification():
    from copy import deepcopy
    from astrid.packs.h3_av.shared.request import H3Request, normalize_request
    from astrid.packs.h3_av.shared.spec import sampling_request
    raw = {"version": 2, "duration": 5, "prompt": "Edit", "media": [
        {"asset": "scene", "role": "timeline", "modality": "video", "at": {"frame": 0}, "range": [0, 5],
         "edit": [{"stream": "audio", "during": [0, 2], "guides": ["fixed"]}]},
        {"id": "fixed", "asset": "voice", "role": "reference", "modality": "audio"},
    ]}
    request = normalize_request(raw)
    forged = deepcopy(request.value)
    forged["media"][1]["use"] = "conditioning"
    with pytest.raises(H3RequestError, match="conflicts with the original audio use"):
        sampling_request(H3Request(forged, request.digest), 0)


def test_default_video_guides_exclude_fixed_audio_occurrence():
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.spec import canonical_spec
    raw = {"version": 2, "duration": 5, "prompt": "Edit", "media": [
        {"asset": "scene", "role": "timeline", "modality": "video", "at": {"frame": 0}, "range": [0, 5],
         "edit": [{"stream": "video", "during": [0, 5]},
                  {"stream": "audio", "during": [0, 2], "guides": ["fixed"]}]},
        {"id": "fixed", "asset": "voice", "role": "reference", "modality": "audio", "range": [0, 2]},
        {"id": "speaker", "asset": "voice", "role": "reference", "modality": "audio", "range": [2, 4]},
    ]}
    spec = canonical_spec(normalize_request(raw))
    selected = {ref.name: ref for ref in spec.refs}
    video_refs = spec.sampling_passes()[0].operation.refs
    assert [selected[name].authored_id for name in video_refs] == ["speaker"]
    raw["media"][0]["edit"][0]["guides"] = ["fixed"]
    with pytest.raises(H3RequestError, match="both conditioning and replacement"):
        normalize_request(raw)


def test_derived_generated_pass_does_not_pre_splice_unselected_fixed_occurrence(tmp_path):
    from astrid.packs.h3_av.shared.compose import _exact_audio_for_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.spec import sampling_request

    raw = {"version": 2, "duration": 5, "prompt": "Edit", "media": [
        {"asset": "scene", "role": "timeline", "modality": "video", "at": {"frame": 0}, "range": [0, 5],
         "edit": [{"stream": "video", "during": [0, 5], "guides": []},
                  {"stream": "audio", "during": [0, 2], "guides": ["fixed"]},
                  {"stream": "audio", "during": [2, 4]}]},
        {"id": "fixed", "asset": "voice", "role": "reference", "modality": "audio", "range": [0, 2]},
    ]}
    derived = sampling_request(normalize_request(raw), 2)
    assert derived.value["media"][1]["use"] == "replacement"
    assert derived.value["media"][0]["edit"][0]["stream"] == "audio"
    # There is no selected fixed clip; pre-splice must not probe either asset.
    missing = tmp_path / "not-needed.mkv"
    assert _exact_audio_for_request({"request": derived.value, "assets": []}, source=missing, generated=missing,
                                    destination=tmp_path / "out.wav", duration=5, source_offset=0,
                                    pre_splice_only=True) is None
