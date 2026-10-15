"""Exhaustive synthetic clocks through the immutable public EVAL implementation.

No browser/render/production inputs. The transport executes the actual warm
metadata handler, not invented evaluation replies. Exact source is configured
explicitly; dependencies are borrowed read-only, never installed here.
"""

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from astrid.core.execution import remotion_evaluation
from astrid.core.execution.remotion_frame_owner import FrameOwnerError
from astrid.core.timeline import scene_answers
from astrid.core.timeline.change_intervals import layer_visibility, revision_intervals

PRODUCER = "0ecbfd6b4440922f92f71985d5511352f3696460"


@pytest.fixture(scope="module")
def bridge():
    root = os.environ.get("ASTRID_CHANGE_VIEW_EVAL_ROOT")
    dependencies = os.environ.get("ASTRID_CHANGE_VIEW_NODE_MODULES")
    if not root or not dependencies:
        pytest.skip(
            "Set explicit immutable EVAL root and existing NODE_MODULES for real evaluation proof"
        )
    assert (
        subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
        == PRODUCER
    )
    assert not subprocess.check_output(
        ["git", "-C", root, "status", "--porcelain"], text=True
    ).strip()
    folder = Path(__file__).parent
    proc = subprocess.Popen(
        [
            "node",
            "--loader",
            str(folder / "change_view_loader.mjs"),
            str(folder / "change_view_bridge.mjs"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    def request(props):
        proc.stdin.write(json.dumps(props) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, f"EVAL subprocess exited {proc.poll()}"
        response = json.loads(line)
        assert response["ok"], response
        return response["evaluation"]

    yield request
    proc.stdin.close()
    proc.wait(timeout=10)
    assert proc.returncode == 0


@pytest.fixture
def owner(bridge, tmp_path, monkeypatch):
    class Transport:
        def request(self, identity, request, parser):
            assert request["compositionId"] == "AstridTrackEvaluation"
            return {
                "ok": True,
                "evaluation": bridge(json.loads(Path(request["propsPath"]).read_text())),
            }

    monkeypatch.setattr(remotion_evaluation, "host_remotion_client", lambda: Transport())
    client = remotion_evaluation.RemotionEvaluationClient(
        project_dir=tmp_path,
        node_executable=Path(subprocess.check_output(["which", "node"], text=True).strip()),
        attempt_root=tmp_path,
        renderer_closure=lambda: PRODUCER,
    )
    previous = scene_answers.use_client(client)
    yield bridge
    scene_answers.use_client(previous)


def clip(id="p", **fields):
    return {
        "id": id,
        "track": "plate",
        "clipType": "media",
        "asset": "photo",
        "at": 1,
        "hold": 1,
        **fields,
    }


def snapshot(*clips, fps=10):
    return {
        "config": {
            "tracks": [
                {"id": "upper", "kind": "visual", "label": "Upper"},
                {"id": "plate", "kind": "visual", "label": "Picture"},
                {"id": "audio", "kind": "audio", "label": "Audio"},
            ],
            "clips": list(clips),
            "theme_overrides": {"visual": {"canvas": {"fps": fps, "width": 200, "height": 100}}},
        },
        "registry": {
            "assets": {
                "photo": {"file": "photo.jpg", "type": "image/jpeg", "resolution": "200x100"},
                "sound": {"file": "sound.wav", "type": "audio/wav"},
            }
        },
    }


def contains(intervals, value):
    return any(a <= value < b for a, b in intervals)


def frames(snap, count=60):
    return scene_answers.frames_at(
        snap["config"],
        list(range(count)),
        10,
        {"width": 200, "height": 100},
        assets=snap["registry"],
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"at": 2.3},
        {"hold": 0.4},
        {"hold": 1.6},
        {"from": 0.4, "to": 2, "hold": None, "speed": 2},
        {"opacity": 0.5},
        {"x": 70},
        {"cropLeft": 0.4},
        {"effects": {"fade_in": 0.4}},
        {"entrance": "slide-up"},
        {"modifiers": [{"id": "mask", "element": "unknown.mask", "params": {}}]},
        {"params": {"color": "red"}},
    ],
)
def test_every_changed_shared_frame_is_enclosed(owner, mutation):
    before = snapshot(clip(), clip("later", at=5, hold=1))
    after = copy.deepcopy(before)
    after["config"]["clips"][0].update(mutation)
    report = revision_intervals(before, after)
    old, new = frames(before), frames(after)
    for frame, (a, b) in enumerate(zip(old, new)):
        # Shared answer changes plus source/param changes on mounted layers.
        if a != b or (
            before["config"]["clips"][0] != after["config"]["clips"][0]
            and any(row["clipId"] == "p" for row in a["layers"] + b["layers"])
        ):
            assert contains(report["frame_intervals"], frame), (mutation, frame, report)
    assert not contains(report["frame_intervals"], 45)


def test_transitive_assets_cycles_track_order_and_disjoint_support(owner):
    before = snapshot(clip(), clip("q", at=4, params={"source": "proxy"}), clip("tail", at=6))
    before["registry"]["assets"]["proxy"] = {"source": "photo"}
    after = copy.deepcopy(before)
    after["registry"]["assets"]["photo"]["file"] = "new.jpg"
    result = revision_intervals(before, after)
    assert result["affected_clips"] == [
        "p",
        "q",
        "tail",
    ]  # all refer to photo directly or transitively
    assert result["frame_intervals"] == [[10, 20], [40, 50], [60, 70]]
    before["config"]["clips"][0]["dependsOn"] = "q"
    before["config"]["clips"][1]["dependsOn"] = "p"
    after = copy.deepcopy(before)
    after["config"]["clips"][0]["opacity"] = 0.5
    assert {"p", "q"} <= set(revision_intervals(before, after)["affected_clips"])
    after = copy.deepcopy(before)
    after["config"]["tracks"].reverse()
    assert contains(revision_intervals(before, after)["frame_intervals"], 15)


def test_shared_coverage_original_fixtures(owner):
    half = snapshot(clip(opacity=0.5), clip("top", track="upper", opacity=0.5))
    answer = frames(half, 21)[15]
    assert answer["coverage"] == "GAP"  # .75 composite alpha cannot prove coverage
    assert all(row["visible"] is True for row in answer["layers"])
    holes = snapshot(clip())
    holes["registry"]["assets"]["photo"]["type"] = "image/png"
    assert layer_visibility(holes, "p", 15)["visible"] == "unknown"
    assert frames(holes, 21)[15]["coverage"] == "UNKNOWN"
    cropped = snapshot(clip(cropLeft=0.5))
    assert frames(cropped, 21)[15]["coverage"] == "GAP"
    masked = snapshot(clip(modifiers=[{"id": "mask", "element": "unknown.mask", "params": {}}]))
    assert layer_visibility(masked, "p", 15)["visible"] == "unknown"
    stale = snapshot(clip(params={"bounds": [0, 0, 200, 100]}, entrance="slide-up"))
    assert layer_visibility(stale, "p", 15)["bounds"]["status"] == "unknown"
    source = snapshot(clip())
    source["registry"]["assets"] = {}
    assert layer_visibility(source, "p", 15)["visible"] is False
    assert layer_visibility(snapshot(clip()), "p", 20)["visible"] is False
    # Explicit registered component + stale cached rectangle remains unknown.
    pure = owner(
        {
            "pure": {
                "tracks": [
                    {
                        "track": {"id": "plate", "kind": "visual"},
                        "clips": [clip(clipType="sprite", params={"bounds": [0, 0, 200, 100]})],
                        "transitions": [],
                    }
                ],
                "assets": source["registry"],
                "fps": 10,
                "width": 200,
                "height": 100,
                "elements": ["sprite"],
                "frames": [15],
            }
        }
    )[0]
    assert pure["layers"][0]["visible"] == "unknown"
    assert pure["layers"][0]["bounds"]["status"] == "unknown"


def test_removed_added_past_end_and_identical(owner):
    before = snapshot(clip(), clip("tail", at=5))
    after = snapshot(clip("tail", at=5), clip("new", at=3))
    result = revision_intervals(before, after)
    assert result["frame_intervals"] == [[10, 20], [30, 40]]
    assert revision_intervals(before, before)["frame_intervals"] == []
    assert revision_intervals(before, before)["audio_sample_intervals"] == []


def test_unknown_dependency_and_skipped_queries_never_disappear(owner, monkeypatch):
    before = snapshot(clip(), clip("tail", at=5))
    after = copy.deepcopy(before)
    after["config"]["clips"][0]["dependsOn"] = "unresolved:source"
    result = revision_intervals(before, after, evaluated_frames=[15])
    assert result["frame_intervals"] == [[10, 20]]
    assert result["skipped_frame_intervals"] == [[10, 15], [16, 20]]
    assert any(f["code"] == "unknown-dependency" for f in result["findings"])

    def unavailable(*a, **kw):
        raise FrameOwnerError("synthetic-owner-down")

    monkeypatch.setattr(scene_answers, "frames_at", unavailable)
    result = revision_intervals(before, after)
    assert result["frame_intervals"] == [[10, 20]]
    assert result["skipped_frame_intervals"] == [[10, 20]]
    assert result["status"] == "unknown"
    assert layer_visibility(after, "p", 15)["visible"] == "unknown"


@pytest.mark.parametrize(
    "curve", ["linear", "equal-power", "exponential-fast", "exponential-slow", "s-curve"]
)
def test_every_changed_audio_sample_uses_public_pcm_envelope(owner, curve):
    rate = 1000
    before_clip = clip(
        "voice",
        track="audio",
        asset="sound",
        at=1.03,
        hold=0.7,
        params={"fadeIn": 0.2, "fadeOut": 0.2, "fadeInCurve": curve, "fadeOutCurve": curve},
    )
    after_clip = copy.deepcopy(before_clip)
    after_clip["params"]["fadeIn"] = 0.5
    before, after = (
        snapshot(before_clip, clip("tail", at=3)),
        snapshot(after_clip, clip("tail", at=3)),
    )
    report = revision_intervals(before, after, sample_rate=rate)
    old = owner({"audio": {"clip": before_clip, "fps": 10, "rate": rate, "length": 700}})
    new = owner({"audio": {"clip": after_clip, "fps": 10, "rate": rate, "length": 700}})
    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    assert changed
    for sample in changed:
        assert contains(report["audio_sample_intervals"], 1000 + sample)
    assert report["audio_sample_intervals"] == [[1000, 1700]]
    assert report["skipped_audio_sample_intervals"] == [[1000, 1700]]


@pytest.mark.parametrize(
    "mutation",
    [
        {"at": 0.35},
        {"hold": 0.2},
        {"hold": 1.2},
        {"speed": 1.7},
        {"volume": 0.25},
        {"asset": "other"},
        {"from": 0.25},
        {"params": {"fadeOut": 0.3}},
        {
            "audio": {
                "envelope": {
                    "keyframes": [{"at": 0, "value": 0.2}, {"at": 0.8, "value": 0.8}],
                    "minimum": [{"keyframes": [{"at": 0, "value": 0.5}]}],
                }
            }
        },
    ],
)
def test_exhaustive_mixed_audio_timing_trim_rate_gain_and_nested_envelope(owner, mutation):
    rate, fps = 997, 10
    voice = clip("voice", track="audio", asset="sound", at=0.15, hold=0.8, volume=0.9)
    changed = {**copy.deepcopy(voice), **mutation}
    overlap = clip("overlap", track="audio", asset="sound", at=0.5, hold=0.5, volume=0.4)
    before = snapshot(voice, overlap, clip("tail", at=2))
    after = snapshot(changed, overlap, clip("tail", at=2))
    result = revision_intervals(before, after, sample_rate=rate)
    old = owner({"mix": {"clips": [voice, overlap], "fps": fps, "rate": rate, "length": 2991}})
    new = owner({"mix": {"clips": [changed, overlap], "fps": fps, "rate": rate, "length": 2991}})
    differences = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    assert differences
    assert all(contains(result["audio_sample_intervals"], n) for n in differences)
    assert not contains(result["audio_sample_intervals"], 1994)


@pytest.mark.parametrize("align", ["start", "center", "end"])
def test_shared_transition_extension_and_native_video_sound(owner, align):
    out = clip(
        "out", at=0, hold=2, transition={"id": "cross-fade", "duration": 0.6, "align": align}
    )
    incoming = clip("in", at=2, hold=2)
    before = snapshot(out, incoming)
    before["registry"]["assets"]["photo"]["type"] = "video/mp4"
    after = copy.deepcopy(before)
    after["config"]["clips"][1]["opacity"] = 0.4
    report = revision_intervals(before, after, sample_rate=997)
    old, new = frames(before, 40), frames(after, 40)
    for frame, (a, b) in enumerate(zip(old, new)):
        if a != b:
            assert contains(report["frame_intervals"], frame)
        if any(layer["clipId"] == "in" for layer in a["layers"] + b["layers"]):
            assert contains(report["audio_sample_intervals"], int(frame / 10 * 997))


def test_unknown_component_dependency_widens_only_its_support(owner):
    before = snapshot(
        clip(), clip("sprite", clipType="text-card", at=4, hold=1), clip("tail", at=6)
    )
    after = copy.deepcopy(before)
    after["config"]["clips"][0]["opacity"] = 0.5
    report = revision_intervals(before, after)
    assert report["frame_intervals"] == [[10, 20], [40, 50]]
    assert report["status"] == "unknown"
    assert any(f["code"] == "unknown-component-dependencies" for f in report["findings"])
    assert not contains(report["frame_intervals"], 65)


@pytest.mark.parametrize("rational", [(10, 1), (30, 1), (24000, 1001)])
@pytest.mark.parametrize("at,hold", [(0.005, 0.035), (0.15, 0.45), (1.035, 0.805)])
def test_exhaustive_fractional_clocks_and_rounding_boundaries(owner, rational, at, hold):
    before = snapshot(clip(at=at, hold=hold), clip("tail", at=3))
    before["fps_rational"] = rational
    after = copy.deepcopy(before)
    after["config"]["clips"][0].update(at=at + 0.14, speed=1.3)
    report = revision_intervals(before, after, sample_rate=997)
    fps = rational[0] / rational[1]
    answers = [
        scene_answers.frames_at(
            s["config"], list(range(120)), fps, {"width": 200, "height": 100}, assets=s["registry"]
        )
        for s in (before, after)
    ]
    for frame, (a, b) in enumerate(zip(*answers)):
        if a != b:
            assert contains(report["frame_intervals"], frame)


def test_blank_duration_change_and_unused_native_asset_negative(owner):
    before = snapshot(clip())
    after = copy.deepcopy(before)
    before["duration_frames"] = 30
    after["duration_frames"] = 40
    result = revision_intervals(before, after, sample_rate=997)
    assert result["frame_intervals"] == [[30, 40]]
    assert result["audio_sample_intervals"] == [[2991, 3988]]
    after = copy.deepcopy(before)
    after["registry"]["assets"]["unused"] = {"file": "unused.jpg", "type": "image/jpeg"}
    assert revision_intervals(before, after)["frame_intervals"] == []


def test_unresolved_bundle_includes_parent_only_layers_and_both_frame_clocks():
    from astrid.core.timeline.change_intervals import intervals_from_bundles

    before = {
        "parent": {
            "config": {
                "clips": [clip(at=4, hold=1)],
                "theme_overrides": {"visual": {"canvas": {"fps": 10}}},
            }
        },
        "placements": [],
        "shots": {},
    }
    after = copy.deepcopy(before)
    after["parent"]["config"]["theme_overrides"]["visual"]["canvas"]["fps"] = 30
    result = intervals_from_bundles(before, after)
    assert result["frame_intervals"] == [[0, 150]]
    assert result["audio_sample_intervals"] == [[0, 240000]]
    assert result["status"] == "unknown"
    assert result["skipped_frame_intervals"] == [[0, 150]]
