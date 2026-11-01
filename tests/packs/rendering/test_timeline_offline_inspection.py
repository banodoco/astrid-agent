"""Bounded evidence lookup, including legacy bundles and hostile payloads."""
import hashlib
import json

import pytest

from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import (
    INSPECTION_MAX_BYTES, compact_render_receipt, inspect_filmstrip,
)


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


def make_bundle(tmp_path, *, count=25, legacy=False, audio=None, extra=None):
    snapshot = {"project_slug": "demo", "timeline_id": "main", "render_run_id": "run",
                "video_digest": "sha256:video", "fps_rational": [30, 1], "duration_frames": 300,
                "clips": [{"id": "clip", "shot_id": "shot", "shot_name": "The shot", "track": "picture", "asset": "asset",
                           "occurrence_id": "occ", "start_frame": 0, "end_frame": 300}],
                "metadata": {"canonical_timeline": {"config_version": 4, "config_hash": "hash", "huge": "x" * 100000}}}
    cards = [{"id": f"frame-{n:09d}", "frame": n, "time_seconds": n / 30,
              "time_rational": [n, 30], "image": f"frames/frame-{n:09d}.jpg", "sample_reasons": ["interval"],
              "clips": snapshot["clips"], "scripts": [{"text": "should not be copied"}]} for n in range(count)]
    (tmp_path / "frames").mkdir()
    for item in cards:
        (tmp_path / item["image"]).write_bytes(b"test image")
    (tmp_path / "render-snapshot.json").write_bytes(encoded(snapshot))
    if audio is not None:
        (tmp_path / "audio-analysis.json").write_bytes(encoded(audio))
    index = {"schema": "astrid.filmstrip.v1", "provenance": snapshot, "cards": cards,
             "audio": audio, "navigation": {"targets": {"huge": "x" * 100000}}, "coverage": {}, "sampling": {}}
    if not legacy:
        index = compact_render_receipt(index, snapshot, tmp_path)
    if extra:
        index.update(extra)
    (tmp_path / "frame-index.json").write_bytes(encoded(index))
    (tmp_path / "filmstrip-001.png").write_bytes(b"page")
    outputs = []
    for path in [tmp_path / item["image"] for item in cards] + [tmp_path / "frame-index.json", tmp_path / "render-snapshot.json", tmp_path / "filmstrip-001.png"]:
        outputs.append({"path": path.relative_to(tmp_path).as_posix(), "bytes": path.stat().st_size,
                        "content_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()})
    if audio is not None:
        path = tmp_path / "audio-analysis.json"
        outputs.append({"path": path.name, "bytes": path.stat().st_size, "content_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(encoded({"kind": "timeline_filmstrip", "outputs": outputs}))
    return manifest, index


def test_new_receipt_is_small_and_points_to_verified_snapshot(tmp_path):
    manifest, index = make_bundle(tmp_path)
    assert len(encoded(index)) < 15000
    assert not {"navigation", "audio", "boundary_index"} & index.keys()
    assert "metadata" not in index["provenance"]
    assert not {"clips", "scripts", "actions"} & index["cards"][0].keys()
    assert index["canonical_timeline"] == {"config_version": 4, "config_hash": "hash", "timeline_ref": "main"}
    assert index["snapshot_sidecar"]["verified"] is True
    assert inspect_filmstrip(manifest)["ok"]


def test_receipt_persists_capture_provenance_and_exact_sampling(tmp_path):
    snapshot = {"project_slug": "demo", "timeline_id": "main", "render_run_id": "run",
                "video_digest": "sha256:video", "fps_rational": [24, 1], "duration_frames": 48}
    index = {
        "cards": [],
        "sampling": {
            "mode": "exact_frame", "range": [0.125, 1 / 6],
            "requested_at": 0.13, "resolved_at_frame": 3,
            "resolved_at_time": 0.125, "rounding_rule": "floor_at_authored_fps",
            "density": {"mode": "every_frames", "value": 1},
            "step_frames_rational": [1, 1], "resolution": [320, 180],
        },
        "coverage": {},
        "frame_capture": {
            "evidence_source": "fresh_capture", "renderer": "renderer-v1",
            "renderer_environment": "{\"sources\":\"digest\"}",
            "fresh_frames": 1, "cached_frames": 0, "requested_frames": [3],
            "resolution": [320, 180],
            "worker": {"batches": 1, "serialized": True},
        },
    }
    receipt = compact_render_receipt(index, snapshot, tmp_path)
    assert receipt["sampling"]["resolved_at_frame"] == 3
    assert receipt["sampling"]["rounding_rule"] == "floor_at_authored_fps"
    assert receipt["sampling"]["step_frames_rational"] == [1, 1]
    assert receipt["sampling"]["resolution"] == [320, 180]
    assert receipt["provenance"]["frame_capture"]["evidence_source"] == "fresh_capture"
    assert receipt["provenance"]["frame_capture"]["requested_frame_bounds"] == [3, 3]
    environment = receipt["provenance"]["frame_capture"]["renderer_environment"]
    assert environment["digest"].startswith("sha256:")
    assert environment["sources"] == "digest"
    assert len(environment["digest"]) == 71


def test_receipt_keeps_component_and_paired_surface_settings(tmp_path):
    snapshot = {
        "project_slug": "demo", "timeline_id": "main", "render_run_id": "run",
        "video_digest": "sha256:video", "fps_rational": [30, 1], "duration_frames": 90,
    }
    receipt = compact_render_receipt(
        {
            "cards": [],
            "components": ["output", "inputs", "text", "audio"],
            "component_request": {
                "show": [], "hide": [],
                "resolved": ["output", "text", "audio", "inputs"], "default": True,
            },
            "sampling": {
                "mode": "interval", "density": {"mode": "every_frames", "value": 6},
                "step_frames_rational": [6, 1], "resolution": [640, 360],
            },
            "coverage": {},
            "static_surface": {
                "schema": "astrid.timeline-static-surface.v2", "mode": "paired_rows",
                "components": ["output", "inputs"], "columns": 5, "page_size": 5,
                "page_count": 1, "row_count": 1, "axis": "linear_half_open_seconds",
                "canonical_input_tracks": ["picture", "audio"],
                "rows": [{
                    "index": 1, "start_seconds": 0.0, "end_seconds": 2.0,
                    "output_card_count": 5, "card_ids": ["frame-1"],
                    "input_tracks": ["picture"],
                }],
            },
        },
        snapshot,
        tmp_path,
    )
    assert receipt["components"] == ["output", "inputs", "text", "audio"]
    assert receipt["component_request"]["resolved"] == ["output", "text", "audio", "inputs"]
    assert receipt["sampling"]["density"] == {"mode": "every_frames", "value": 6}
    assert receipt["sampling"]["step_frames_rational"] == [6, 1]
    assert receipt["sampling"]["resolution"] == [640, 360]
    assert receipt["static_surface"]["mode"] == "paired_rows"
    assert receipt["static_surface"]["rows"][0]["input_tracks"] == ["picture"]


def test_summary_exposes_sampling_and_surface_contract(tmp_path):
    manifest, _ = make_bundle(
        tmp_path,
        count=1,
        extra={
            "components": ["output", "inputs"],
            "component_request": {"resolved": ["output", "inputs"], "default": True},
            "sampling": {
                "mode": "interval", "density": {"mode": "every_frames", "value": 6},
                "step_frames_rational": [6, 1], "resolution": [640, 360],
            },
            "static_surface": {
                "mode": "paired_rows", "columns": 5, "page_count": 1,
                "canonical_input_tracks": ["picture"],
            },
        },
    )

    summary = inspect_filmstrip(manifest, section="summary")
    record = summary["data"]["records"][0]
    assert record["components"] == ["output", "inputs"]
    assert record["sampling"]["step_frames_rational"] == [6, 1]
    assert record["sampling"]["resolution"] == [640, 360]
    assert record["static_surface"]["mode"] == "paired_rows"


@pytest.mark.parametrize("legacy", [False, True])
def test_pagination_and_exact_selectors_read_without_mutation(tmp_path, legacy):
    manifest, _ = make_bundle(tmp_path, legacy=legacy)
    before = (tmp_path / "frame-index.json").read_bytes()
    first = inspect_filmstrip(manifest, section="cards")
    assert [row["frame"] for row in first["data"]["records"]] == list(range(10))
    second = inspect_filmstrip(manifest, section="cards", cursor=first["data"]["next_cursor"])
    assert [row["frame"] for row in second["data"]["records"]] == list(range(10, 20))
    assert inspect_filmstrip(manifest, section="cards", limit=11, cursor=first["data"]["next_cursor"])["error"]["code"] == "invalid_cursor"
    selected = inspect_filmstrip(manifest, section="cards", frame=7, shot="The shot", occurrence="occ", clip="clip", asset="asset", track="picture")
    assert selected["data"]["records"][0]["frame"] == 7
    assert (tmp_path / "frame-index.json").read_bytes() == before


def test_range_and_boundaries_do_not_invent_captured_frames(tmp_path):
    manifest, _ = make_bundle(tmp_path)
    result = inspect_filmstrip(manifest, section="cards", range_value="0.1..0.2")
    assert [row["frame"] for row in result["data"]["records"]] == [3, 4, 5]
    result = inspect_filmstrip(manifest, section="boundaries", clip="clip")
    assert result["data"]["records"][1]["boundary_frame"] == 300
    assert result["data"]["records"][1]["before_captured"] is False
    assert result["data"]["records"][1]["after_captured"] is False


@pytest.mark.parametrize("query,code", [
    ({"section": "all"}, "invalid_query"), ({"limit": 51}, "invalid_query"),
    ({"frame": -1}, "invalid_selector"), ({"clip": "x" * 10000}, "invalid_selector"),
    ({"section": "cards", "range_value": "4..3"}, "invalid_query"),
    ({"section": "pages", "clip": "clip"}, "unsupported_selector"),
    ({"cursor": "x" * 10000}, "invalid_cursor"),
])
def test_errors_are_typed_bounded_and_do_not_echo_input(tmp_path, query, code):
    manifest, _ = make_bundle(tmp_path)
    result = inspect_filmstrip(manifest, **query)
    assert result["error"]["code"] == code
    assert len(encoded(result)) < 1024
    assert "x" * 100 not in encoded(result).decode()


def test_integrity_and_member_escape_are_rejected(tmp_path):
    manifest, _ = make_bundle(tmp_path)
    (tmp_path / "frames/frame-000000000.jpg").write_bytes(b"tampered")
    assert inspect_filmstrip(manifest, section="cards")["error"]["code"] == "integrity_mismatch"
    document = json.loads(manifest.read_bytes())
    document["outputs"][0]["path"] = "../outside.json"
    manifest.write_bytes(encoded(document))
    assert inspect_filmstrip(manifest)["error"]["code"] == "unsafe_member"


def test_unicode_and_inline_payloads_never_escape_the_audio_projection(tmp_path):
    audio = {"status": "analyzed", "render_digest": "sha256:video", "stream": {"sample_rate": 30},
             "waveform": {"levels": [{"bins": [{"index": n, "start_sample": n, "end_sample": n + 1, "rms": 0.1} for n in range(200)]}]},
             "quiet_gaps": [{"start": [0, 1], "end": [1, 1], "measurement": "low_amplitude"}],
             "speech": {"phrases": [{"start": 0, "end": 1, "text": "界" * 10000},
                                     {"start": 1, "end": 2, "text": "data:image/png;inline-payload"}]}}
    manifest, _ = make_bundle(tmp_path, audio=audio)
    rows = []
    cursor = None
    while True:
        result = inspect_filmstrip(manifest, section="audio", limit=50, cursor=cursor)
        assert result["ok"], result
        assert len(encoded(result)) <= INSPECTION_MAX_BYTES
        assert "inline-payload" not in encoded(result).decode()
        rows += result["data"]["records"]
        cursor = result["data"]["next_cursor"]
        if cursor is None:
            break
    assert len([row for row in rows if row.get("kind") == "waveform_bin"]) == 128
    assert any(row.get("truncated") for row in rows)
    window = inspect_filmstrip(manifest, section="audio", range_value="3..4", limit=50)
    assert all(3 <= row["start_seconds"] < 4 for row in window["data"]["records"] if row.get("kind") == "waveform_bin")


def test_receipt_rejects_identity_overflow_instead_of_copying_raw_objects(tmp_path):
    snapshot = {"fps_rational": [30, 1]}
    cards = [{"id": "frame", "image": "frames/frame.jpg", "time_rational": [0, 1],
              "clips": [{"id": "x" * 1000}]}]
    with pytest.raises(ValueError, match="identity limit"):
        compact_render_receipt({"cards": cards}, snapshot, tmp_path)


def test_cli_does_not_bootstrap_runtime_and_errors_stay_small(tmp_path, monkeypatch, capsys):
    from astrid.core.gateway import dispatch
    manifest, _ = make_bundle(tmp_path)
    monkeypatch.setattr(dispatch, "_dispatch_product", lambda *args: pytest.fail("runtime bootstrap attempted"))
    assert dispatch._dispatch_timelines(["inspect", "--manifest", str(manifest), "--section", "cards", "--frame", "4"]) == 0
    output = capsys.readouterr().out
    assert len(output.encode()) <= INSPECTION_MAX_BYTES
    assert json.loads(output)["data"]["records"][0]["frame"] == 4
    assert dispatch._dispatch_timelines(["inspect", "--manifest", str(manifest), "--bogus", "x" * 10000]) == 2
    assert len(capsys.readouterr().out.encode()) < 1024
