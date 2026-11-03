from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from tests.lanes import unavailable


TOOLS_ROOT = Path(__file__).resolve().parents[3]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from astrid.core import timeline  # noqa: E402


def _clip_duration_sec(clip: dict[str, object]) -> float:
    start = float(clip.get("from_", clip.get("from", 0.0)) or 0.0)
    end = float(clip.get("to", start) or start)
    hold = float(clip.get("hold", 0.0) or 0.0)
    return max(0.0, end - start) + max(0.0, hold)


def _visual_intervals(timeline_payload: dict[str, object]) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for clip in timeline_payload["clips"]:
        if clip.get("track") not in {"v1", "v2"} or clip.get("clipType") == "text":
            continue
        start = float(clip.get("at", 0.0))
        end = start + _clip_duration_sec(clip)
        intervals.append((start, end))
    return sorted(intervals)


def _assert_visual_coverage(intervals: list[tuple[float, float]], total_duration: float) -> None:
    assert intervals, "expected at least one non-text visual clip"
    cursor = 0.0
    for start, end in intervals:
        assert start - cursor <= 0.2, f"visual gap exceeds 0.2s before {start:.2f}s"
        cursor = max(cursor, end)
    assert total_duration - cursor <= 0.2, f"visual coverage ends {total_duration - cursor:.2f}s early"


def _assert_hype_cut_invariants(brief_dir: Path) -> None:
    arrangement = timeline.load_arrangement(brief_dir / "arrangement.json")
    timeline_payload = timeline.load_timeline(brief_dir / "hype.timeline.json")
    _ = timeline.load_metadata(brief_dir / "hype.metadata.json")

    audio_ranges_by_pool: dict[str, list[tuple[float, float, int]]] = {}
    arrangement_by_order = {int(clip["order"]): clip for clip in arrangement["clips"]}
    for arrangement_clip in arrangement["clips"]:
        audio_source = arrangement_clip.get("audio_source")
        if not isinstance(audio_source, dict):
            continue
        trim_start, trim_end = map(float, audio_source["trim_sub_range"])
        audio_ranges_by_pool.setdefault(str(audio_source["pool_id"]), []).append(
            (trim_start, trim_end, int(arrangement_clip["order"]))
        )
    for pool_id, ranges in audio_ranges_by_pool.items():
        previous = None
        for trim_start, trim_end, order in sorted(ranges, key=lambda item: item[0]):
            if previous is not None:
                prev_start, prev_end, prev_order = previous
                assert prev_end <= trim_start + 1e-3, (
                    f"audio trim overlap on pool_id {pool_id}: "
                    f"orders {prev_order} and {order} overlap "
                    f"([{prev_start:.3f}, {prev_end:.3f}] vs [{trim_start:.3f}, {trim_end:.3f}])"
                )
            previous = (trim_start, trim_end, order)

    audio_clips = [clip for clip in timeline_payload["clips"] if clip.get("track") == "a1"]
    audio_total = sum(_clip_duration_sec(clip) for clip in audio_clips)
    assert 75.0 <= audio_total <= 90.0, f"a1 total duration {audio_total:.2f}s is outside 75-90s"

    for clip in audio_clips:
        duration = _clip_duration_sec(clip)
        assert 4.0 <= duration <= 10.0, f"{clip['id']} dialogue duration {duration:.2f}s is outside 4-10s"

    clips_by_id = {str(clip["id"]): clip for clip in timeline_payload["clips"]}
    for arrangement_clip in arrangement["clips"]:
        order = int(arrangement_clip["order"])
        role = arrangement_clip["visual_source"]["role"]
        if role != "stinger":
            continue
        visual_clip = clips_by_id.get(f"clip_v2_{order}")
        assert visual_clip is not None, f"missing rendered stinger visual clip for arrangement order {order}"
        duration = _clip_duration_sec(visual_clip)
        assert 2.0 <= duration <= 5.0, f"stinger clip_v2_{order} duration {duration:.2f}s is outside 2-5s"

    for order, arrangement_clip in arrangement_by_order.items():
        visual_source = arrangement_clip.get("visual_source")
        if not isinstance(visual_source, dict) or visual_source.get("role") != "overlay":
            continue
        audio_source = arrangement_clip.get("audio_source")
        assert isinstance(audio_source, dict), f"overlay arrangement order {order} is missing audio_source"
        overlay_clip = clips_by_id.get(f"clip_v2_{order}")
        assert overlay_clip is not None, f"missing rendered overlay clip for arrangement order {order}"
        audio_duration = float(audio_source["trim_sub_range"][1]) - float(audio_source["trim_sub_range"][0])
        required_duration = min(4.0, audio_duration)
        overlay_duration = _clip_duration_sec(overlay_clip)
        assert overlay_duration >= required_duration - 0.05, (
            f"overlay clip_v2_{order} duration {overlay_duration:.2f}s is below "
            f"minimum {required_duration:.2f}s"
        )

    visual_intervals = _visual_intervals(timeline_payload)
    total_duration = audio_total or max(end for _, end in visual_intervals)
    _assert_visual_coverage(visual_intervals, total_duration)

    for clip in timeline_payload["clips"]:
        if clip.get("track") not in {"v1", "v2"} or clip.get("clipType") == "text":
            continue
        visible = max(
            0.0,
            float(clip.get("to", clip.get("from_", clip.get("from", 0.0))) or 0.0)
            - float(clip.get("from_", clip.get("from", 0.0)) or 0.0),
        )
        hold = max(0.0, float(clip.get("hold", 0.0) or 0.0))
        total = visible + hold
        ratio = hold / total if total > 0 else 0.0
        assert ratio < 0.2, f"{clip['id']} freeze ratio {ratio:.2%} must stay below 20%"

    debug_dir = brief_dir.parent.parent / "_llm_debug"
    assert list(debug_dir.glob("arrange.*.request.json")), f"missing arrange request debug files in {debug_dir}"
    assert list(debug_dir.glob("arrange.*.response.json")), f"missing arrange response debug files in {debug_dir}"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _build_synthetic_run(tmp_path: Path) -> Path:
    brief_dir = tmp_path / "runs" / "demo" / "briefs" / "launch"
    debug_dir = brief_dir.parent.parent / "_llm_debug"

    clips: list[dict[str, object]] = []
    timeline_clips: list[dict[str, object]] = []
    metadata_clips: dict[str, dict[str, object]] = {}
    current_at = 0.0

    for order in range(1, 11):
        role = "primary" if order % 2 else "overlay"
        trim_start = float((order - 1) * 10)
        trim_end = trim_start + 7.5
        clips.append(
            {
                "order": order,
                "uuid": f"{order:08x}",
                "audio_source": {"pool_id": f"pool_d_{order:04d}", "trim_sub_range": [trim_start, trim_end]},
                "visual_source": {"pool_id": f"pool_v_{order:04d}", "role": role},
                "text_overlay": None,
                "rationale": f"Beat {order} advances the promo arc.",
            }
        )
        timeline_clips.append(
            {
                "id": f"clip_a_{order}",
                "at": current_at,
                "track": "a1",
                "clipType": "media",
                "asset": "main",
                "from": trim_start,
                "to": trim_end,
                "source_uuid": f"{order:08x}",
            }
        )
        visual_track = "v1" if role == "primary" else "v2"
        timeline_clips.append(
            {
                "id": f"clip_{visual_track}_{order}",
                "at": current_at,
                "track": visual_track,
                "clipType": "media",
                "asset": "main",
                "from": 100.0 + trim_start,
                "to": 100.0 + trim_end,
                "volume": 0.0,
                "source_uuid": f"{order:08x}",
            }
        )
        metadata_clips[f"clip_a_{order}"] = {"source_uuid": f"{order:08x}"}
        metadata_clips[f"clip_{visual_track}_{order}"] = {"source_uuid": f"{order:08x}"}
        current_at += 7.5

    clips.append(
        {
            "order": 11,
            "uuid": "0000000b",
            "audio_source": None,
            "visual_source": {"pool_id": "pool_v_9999", "role": "stinger"},
            "text_overlay": None,
            "rationale": "Short stinger lands the finish cleanly.",
        }
    )
    timeline_clips.append(
        {
            "id": "clip_v2_11",
            "at": current_at,
            "track": "v2",
            "clipType": "media",
            "asset": "main",
            "from": 300.0,
            "to": 304.0,
            "volume": 0.0,
            "source_uuid": "0000000b",
        }
    )
    metadata_clips["clip_v2_11"] = {"source_uuid": "0000000b"}

    arrangement_payload = {
        "version": timeline.ARRANGEMENT_VERSION,
        "generated_at": "2026-04-22T12:00:00Z",
        "brief_text": "Synthetic invariant fixture.",
        "target_duration_sec": 79.0,
        "source_slug": "demo",
        "brief_slug": "launch",
        "pool_sha256": "a" * 64,
        "brief_sha256": "b" * 64,
        "clips": clips,
    }
    timeline_payload = {
        "theme": "banodoco-default",
        "tracks": [
            {"id": "v1", "kind": "visual", "label": "Primary"},
            {"id": "v2", "kind": "visual", "label": "Overlay"},
            {"id": "a1", "kind": "audio", "label": "Dialogue"},
        ],
        "clips": timeline_clips,
    }
    metadata_payload = {
        "version": timeline.METADATA_VERSION,
        "generated_at": "2026-04-22T12:00:00Z",
        "pipeline": {"steps_run": ["arrange", "cut"], "tool_versions": {}, "config_snapshot": {}},
        "clips": metadata_clips,
        "sources": {"main": {}},
    }

    _write_json(brief_dir / "arrangement.json", arrangement_payload)
    _write_json(brief_dir / "hype.timeline.json", timeline_payload)
    _write_json(brief_dir / "hype.metadata.json", metadata_payload)
    _write_json(debug_dir / "arrange.0001.request.json", {"model": "stub"})
    _write_json(debug_dir / "arrange.0001.response.json", {"clips": []})
    (debug_dir / "index.jsonl").write_text('{"stage":"arrange","seq":1}\n', encoding="utf-8")
    return brief_dir


def _discovered_brief_dirs() -> list[Path]:
    explicit = os.environ.get("HYPE_BRIEF_DIR")
    if explicit:
        return [Path(explicit).resolve()]
    if os.environ.get("HYPE_DISCOVER_RUNS") != "1":
        return []
    runs_dir = TOOLS_ROOT / "runs"
    if not runs_dir.is_dir():
        return []
    discovered: list[Path] = []
    for path in sorted(runs_dir.glob("*/briefs/*")):
        if not (
            (path / "arrangement.json").is_file()
            and (path / "hype.timeline.json").is_file()
            and (path / "hype.metadata.json").is_file()
        ):
            continue
        debug_dir = path.parent.parent / "_llm_debug"
        if not list(debug_dir.glob("arrange.*.request.json")) or not list(debug_dir.glob("arrange.*.response.json")):
            continue
        try:
            timeline.load_arrangement(path / "arrangement.json")
        except Exception:
            continue
        discovered.append(path.resolve())
    return discovered


def test_hype_cut_invariants_on_synthetic_run(tmp_path: Path) -> None:
    _assert_hype_cut_invariants(_build_synthetic_run(tmp_path))


@pytest.mark.integration
@pytest.mark.opt_in
def test_hype_cut_invariants_on_discovered_run_dirs() -> None:
    brief_dirs = _discovered_brief_dirs()
    if not brief_dirs:
        unavailable(
            "No arrangement-mode brief directories selected; set HYPE_BRIEF_DIR to target one "
            "explicitly or HYPE_DISCOVER_RUNS=1 to scan local tools/runs output."
        )
    for brief_dir in brief_dirs:
        _assert_hype_cut_invariants(brief_dir)
