"""Fresh public presenter facts carried into the existing lipsync consumer."""

import hashlib
import json
import math
import shutil
import subprocess
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.rendering import scene_answers
from astrid.core.timeline.cuts import find_cut
from astrid.packs.rendering.actions.timeline_visualize.composed_frame import CaptureSceneLabels
from astrid.packs.rendering.actions.timeline_visualize.layers import builtin
from astrid.packs.rendering.actions.timeline_visualize.motion import model, sheet
from tests.packs.rendering import test_presenter_crop_equivalence as crop_helpers
from tests.packs.rendering.test_presenter_crop_equivalence import png_crop, public_box
from tests.packs.rendering.test_presenter_scene_capability import (
    presenter_query as _presenter_query,
)

presenter_query = _presenter_query
pytestmark = pytest.mark.integration
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/presenter_selected_cut"
EXPECTED_HASHES = {
    "inspection.json": "eabcc179baa4b97b6bdf1c8343ca270ef090e1306ab8791ee32787c3226385af",
    "frame-0.png": "6c3c819b5ac4a1b1848e3b72338e1e213e9159b6325eb1068f334dcfffc79ed5",
    "frame-3.png": "76715e2b9c0df4f57e5fb10d992011dbb0536534e4ac27a6e5e3797e12b04c73",
    "frame-4.png": "6c3c819b5ac4a1b1848e3b72338e1e213e9159b6325eb1068f334dcfffc79ed5",
    "frame-5.png": "09b32c84599f3633fa5cf611216e25e667965041f971ab868f76bec354454108",
}


@pytest.fixture(scope="module")
def retained():
    def hashes():
        return {name: hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest()
                for name in EXPECTED_HASHES}

    assert json.loads((FIXTURE / "sha256.json").read_text()) == EXPECTED_HASHES
    assert hashes() == EXPECTED_HASHES
    case = json.loads((FIXTURE / "inspection.json").read_text())
    assert case["frames"] == [0, 3, 4, 5]
    assert len(case["rows"]) == 1
    for frame in case["frames"]:
        with Image.open(FIXTURE / f"frame-{frame}.png") as image:
            assert image.format == "PNG" and image.size == tuple(case["size"]) == (640, 360)
    yield case
    assert hashes() == EXPECTED_HASHES


@pytest.fixture(scope="module")
def public_capture_rows(presenter_query, retained):
    # Reuse the actual registered public node against the retained four-still
    # Root. The geometry helper is unchanged; this entry never imports a renderer.
    helper = Path(crop_helpers.__file__)
    shutil.copyfile(helper.with_name("presenter_crop_geometry.mjs"),
                    presenter_query.project / "presenter_crop_geometry.mjs")
    shutil.copyfile(Path(__file__).with_name("presenter_selected_cut_query.mjs"),
                    presenter_query.project / "presenter_selected_cut_query.mjs")
    result = subprocess.run([str(presenter_query.node), "presenter_selected_cut_query.mjs",
                             str(FIXTURE / "inspection.json")],
                            cwd=presenter_query.project, env=presenter_query.env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    answer = json.loads(result.stdout)
    assert answer["controls"] == ["missing-compiled-id", "ambiguous-compiled-id",
                                  "foreign-owner", "stale-params"]
    assert [row["frame"] for row in answer["answers"]] == retained["frames"]
    return answer["answers"]


@pytest.fixture(scope="module")
def public_zero(presenter_query, retained):
    # Preserve the original selected-cut qualification for the legacy
    # comparator controls; the consumer test below separately reads all four
    # actual registered public rows.
    helper = Path(crop_helpers.__file__)
    shutil.copyfile(helper.with_name("presenter_crop_geometry.mjs"),
                    presenter_query.project / "presenter_crop_geometry.mjs")
    shutil.copyfile(Path(__file__).with_name("presenter_selected_cut_query.mjs"),
                    presenter_query.project / "presenter_selected_cut_query.mjs")
    result = subprocess.run([str(presenter_query.node), "presenter_selected_cut_query.mjs",
                             str(FIXTURE / "inspection.json")],
                            cwd=presenter_query.project, env=presenter_query.env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    answer = json.loads(result.stdout)
    assert answer["controls"] == ["missing-compiled-id", "ambiguous-compiled-id",
                                   "foreign-owner", "stale-params"]
    assert answer["zero"] == {k: v for k, v in retained["rows"][0].items() if k != "erase"}
    return answer["zero"]


def _public_provider(case, rows, monkeypatch, *, mode="fresh"):
    props = deepcopy(case["props"])
    rows = deepcopy(rows)
    by_frame = {row["frame"]: row for row in rows}
    monkeypatch.setattr(scene_answers, "host_frames_at",
                        lambda _props, _basis, frames: [by_frame[frame] for frame in frames])
    capture = CaptureSceneLabels(source_identity="fresh-presenter", props=props,
                                 basis=case["basis"], addresses={"presenter": "presenter"})
    capture.observe(props, tuple(case["frames"]))
    capture.retain({frame: FIXTURE / f"frame-{frame}.png" for frame in case["frames"]})

    class Provider:
        source_identity = "fresh-presenter"
        fresh_scene_frames = (set(case["frames"]) if mode == "fresh"
                              else {case["frames"][0]} if mode == "mixed" else set())
        _cached_scenes = ({frame: capture for frame in case["frames"]} if mode == "cache"
                          else {frame: capture for frame in case["frames"][1:]} if mode == "mixed" else {})

        def scene_for_frame(self, frame):
            return capture if frame in by_frame else None

    return Provider()


def context(case, order=(0, 3, 4, 5), frame_provider=None):
    # The original Root timeline is already flat; do not invent occurrence IDs,
    # clip aliases, neighbouring clips, extra captures or timing for this case.
    snapshot = {**deepcopy(case["props"]["timeline"]),
                "config": deepcopy(case["props"]["timeline"]), "fps_rational": [30, 1]}
    cut = sheet.select_cut(snapshot, "presenter", case["basis"]["fps"])
    frames = {f: FIXTURE / f"frame-{f}.png" for f in order}
    return sheet.build_context(snapshot, cut, frames, fps=case["basis"]["fps"],
                               frame_size=tuple(case["size"]), frame_provider=frame_provider)


@pytest.mark.parametrize("mode", ["fresh", "cache"])
def test_single_mode_public_mouth_facts_drive_existing_consumer(public_capture_rows, retained, monkeypatch, mode):
    provider = _public_provider(retained, public_capture_rows, monkeypatch, mode=mode)
    ctx = context(retained, frame_provider=provider)
    presenter = ctx.elements[0]
    assert ctx.shared["public_scene_evidence_mode"] == ("fresh_capture" if mode == "fresh" else "cache")
    assert builtin._public_mouth_facts(ctx, presenter, [0, 3, 4, 5])["rect"] == (894.0, 606.0, 138.0, 48.0)
    monkeypatch.setattr(model, "mouth_box", lambda *_args: pytest.fail("public ROI used private crop"))
    result = builtin.render_lipsync(ctx)
    assert "LIPSYNC pixels: mouth region changed between 3 of 3 sampled frame pairs" in result.findings


def test_public_mouth_rows_drive_curve_and_missing_row_keeps_private_fallback(
        public_capture_rows, retained, monkeypatch):
    provider = _public_provider(retained, public_capture_rows, monkeypatch)
    ctx = context(retained, frame_provider=provider)
    presenter = ctx.elements[0]
    public_states = {row["frame"]: {0.0: "closed", 0.5: "half", 1.0: "open"}[
        row["layers"][0]["diagnostics"]["motion"]["mouth"]]
                     for row in public_capture_rows}
    original_mouth_state = model.mouth_state
    private_missing = original_mouth_state(presenter, 1, ctx.fps)
    samples = {}

    def private_state(element, frame, fps):
        if element.id == presenter.id and frame in public_states:
            pytest.fail("admitted public mouth row consulted private mouth_state")
        return original_mouth_state(element, frame, fps)

    original_props_at = model.props_at

    def record_props(element, t, fps, **kwargs):
        props = original_props_at(element, t, fps, **kwargs)
        if element.id == presenter.id and "mouth" in props:
            samples[int(math.floor(t * fps))] = props["mouth"]
        return props

    monkeypatch.setattr(model, "mouth_state", private_state)
    monkeypatch.setattr(model, "props_at", record_props)
    builtin.render_curves(ctx)

    assert {frame: samples[frame] for frame in public_states} == {
        frame: {"closed": 0.0, "half": 0.5, "open": 1.0}[state]
        for frame, state in public_states.items()
    }
    assert samples[1] == {"closed": 0.0, "half": 0.5, "open": 1.0}[private_missing]


def test_missing_presenter_override_preserves_props_dispatch(retained, monkeypatch):
    presenter = context(retained).elements[0]
    dispatched = {"custom": 1.0}
    monkeypatch.setitem(model.PROPS, presenter.type,
                        lambda _element, _frame, _fps: dispatched)

    assert model.props_at(presenter, 1 / 30 + 1e-6, 30) == dispatched
    assert model.props_at(presenter, 1 / 30 + 1e-6, 30,
                          presenter_mouth_state="closed")["mouth"] == 0.0


def test_mixed_public_mouth_facts_keep_private_fallback(public_capture_rows, retained, monkeypatch):
    provider = _public_provider(retained, public_capture_rows, monkeypatch, mode="mixed")
    mixed = context(retained, frame_provider=provider)
    private = context(retained)
    presenter = mixed.elements[0]
    assert mixed.shared["public_scene"] == {}
    assert mixed.shared["public_scene_evidence_mode"] is None
    assert builtin._public_mouth_facts(mixed, presenter, [0, 3, 4, 5]) is None
    mixed_result = builtin.render_lipsync(mixed)
    private_result = builtin.render_lipsync(private)
    assert mixed_result.findings == private_result.findings
    assert mixed_result.image.tobytes() == private_result.image.tobytes()


@pytest.mark.parametrize("raw_mouth", [None, {"x": 151, "y": 104}])
def test_partial_or_default_raw_mouth_keeps_private_fallback(public_capture_rows, retained, raw_mouth):
    case = deepcopy(retained)
    params = case["props"]["timeline"]["clips"][0]["params"]
    if raw_mouth is None:
        params.pop("mouth", None)
    else:
        params["mouth"] = raw_mouth
    ctx = context(case)
    ctx.shared["public_scene"] = {
        row["frame"]: {"answer": row, "basis": {"width": 1920, "height": 1080, "fps": 30},
                        "addresses": {"presenter": "presenter"}}
        for row in public_capture_rows
    }
    assert builtin._public_mouth_facts(ctx, ctx.elements[0], [0, 3, 4, 5]) is None


@pytest.mark.parametrize("mutation", ["stale-clip", "forged-roi", "missing-row"])
def test_planted_public_fact_failures_keep_private_fallback(public_capture_rows, retained, mutation):
    rows = deepcopy(public_capture_rows)
    if mutation == "stale-clip":
        rows[0]["layers"][0]["clipId"] = "stale-presenter"
    elif mutation == "forged-roi":
        rows[0]["layers"][0]["diagnostics"]["mouth"]["rect"] = "forged"
    else:
        rows.pop()
    ctx = context(retained)
    ctx.shared["public_scene"] = {
        row["frame"]: {"answer": row, "basis": {"width": 1920, "height": 1080, "fps": 30},
                        "addresses": {"presenter": "presenter"}}
        for row in rows
    }
    assert builtin._public_mouth_facts(ctx, ctx.elements[0], [0, 3, 4, 5]) is None


def cut_ref(ctx):
    return {key: ctx.cut[key] for key in
            ("index", "clip_id", "occurrence_id", "shot_id", "track", "start", "end")}


def statistic(ctx, selected, reads, pairs, changed, fractions, finding):
    return {"cut": cut_ref(ctx), "selected": selected, "reads": reads, "pairs": pairs,
            "changed_pixels": changed, "fractions": fractions,
            "changes": sum(f > .02 for f in fractions),
            "denominator": len(selected) - 1 if len(selected) >= 3 else None,
            "finding": finding}


def private_statistic(ctx):
    """Observe the actual builtin without replacing private math or pixels."""
    reads, pairs, changed, fractions = [], [], [], []
    frame_image, diff = ctx.frame_image, builtin._diff_fraction

    def read(frame):
        reads.append(frame)
        return frame_image(frame)

    def compare(a, b):
        fraction, mask = diff(a, b)
        assert a.size == b.size == (47, 17)
        pairs.append(reads[-2:])
        changed.append(mask.histogram()[255])
        fractions.append(fraction)
        return fraction, mask

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ctx, "frame_image", read)
        patch.setattr(builtin, "_diff_fraction", compare)
        result = builtin.render_lipsync(ctx)
    findings = [s for s in result.findings if s.startswith("LIPSYNC pixels:")]
    selected = [f for f in builtin._sorted_frames(ctx)
                if float(ctx.cut["start"]) <= f / ctx.fps < float(ctx.cut["end"])]
    assert len(findings) <= 1
    return statistic(ctx, selected, reads, pairs, changed, fractions, findings[0] if findings else None)


def public_statistic(ctx, zero):
    """Reference ROI adapter; reuse the current comparator/conversion.

    This preserves the builtin's selected-key denominator and resets adjacency
    on a missing image. It intentionally makes no semantic-mouth-state query.
    """
    selected = [f for f in builtin._sorted_frames(ctx)
                if float(ctx.cut["start"]) <= f / ctx.fps < float(ctx.cut["end"])]
    reads, pairs, changed, fractions = [], [], [], []
    finding = None
    crop = png_crop(public_box(zero), ctx.frame_size)
    if len(selected) >= 3:
        previous = None
        for frame in selected:
            reads.append(frame)
            image = ctx.frame_image(frame)
            region = image.crop(crop) if image is not None else None
            if region is not None and previous is not None:
                fraction, mask = builtin._diff_fraction(previous[1], region)
                pairs.append([previous[0], frame])
                changed.append(mask.histogram()[255])
                fractions.append(fraction)
            previous = (frame, region) if region is not None else None
        changes = sum(f > .02 for f in fractions)
        finding = (f"LIPSYNC pixels: mouth region changed between {changes} of "
                   f"{len(selected) - 1} sampled frame pairs")
    return statistic(ctx, selected, reads, pairs, changed, fractions, finding)


def test_actual_selected_cut_aggregate(retained, public_zero):
    ctx = context(retained)
    before = deepcopy(dict(ctx.cut))
    assert ctx.cut == find_cut(ctx.cuts, "1") == find_cut(ctx.cuts, "@0")
    assert cut_ref(ctx) == {"index": 1, "clip_id": "presenter", "occurrence_id": "",
                            "shot_id": "", "track": "overlay", "start": 0, "end": 2}
    presenter = ctx.elements[0]
    assert presenter.id == public_zero["clipId"] == "presenter"
    # No attached maths/semantic double: this is the caller's current fallback.
    assert presenter.maths is None
    old = png_crop(model.mouth_box(presenter, 0), ctx.frame_size)
    assert old == png_crop(public_box(public_zero), ctx.frame_size) == (298, 202, 345, 219)
    for f in retained["frames"]:
        image = ctx.frame_image(f)
        assert image.crop(old).tobytes() == image.crop(png_crop(public_box(public_zero), ctx.frame_size)).tobytes()
    actual = private_statistic(ctx)
    assert actual == public_statistic(ctx, public_zero)
    assert actual["selected"] == actual["reads"] == [0, 3, 4, 5]
    assert actual["pairs"] == [[0, 3], [3, 4], [4, 5]]
    assert actual["changed_pixels"] == [336, 336, 336]
    assert actual["fractions"] == [336 / 799] * 3
    assert (actual["changes"], actual["denominator"]) == (3, 3)
    assert actual["finding"] == "LIPSYNC pixels: mouth region changed between 3 of 3 sampled frame pairs"
    assert ctx.cut == before
    print("ACTUAL_SELECTED_CUT=" + json.dumps(actual, sort_keys=True))


@pytest.mark.parametrize("order", [(5, 0, 4, 3), (5, 3, 0, 3, 4)])
def test_order_and_duplicate_frame_keys(retained, public_zero, order):
    # A repeated request references the same real frame-3 file; it is not a new
    # capture. Both the capture API and LayerContext use unique frame keys.
    actual = private_statistic(context(retained, order))
    assert actual == private_statistic(context(retained))
    assert actual == public_statistic(context(retained, order), public_zero)


def test_missing_key_changes_adjacency_and_denominator(retained, public_zero):
    ctx = context(retained, (0, 4, 5))
    actual = private_statistic(ctx)
    assert actual == public_statistic(ctx, public_zero)
    assert actual["pairs"] == [[0, 4], [4, 5]]
    # 0 and 4 are independent retained captures with identical original bytes.
    assert actual["changed_pixels"] == [0, 336]
    assert actual["fractions"] == [0, 336 / 799]
    assert (actual["changes"], actual["denominator"]) == (1, 2)
    assert actual["finding"] == "LIPSYNC pixels: mouth region changed between 1 of 2 sampled frame pairs"


@pytest.mark.parametrize("missing,pairs", [(3, [[4, 5]]), (4, [[0, 3]])])
def test_missing_image_resets_pairs_but_retains_key_denominator(retained, public_zero, monkeypatch, missing, pairs):
    ctx = context(retained)
    read = ctx.frame_image
    monkeypatch.setattr(ctx, "frame_image", lambda f: None if f == missing else read(f))
    actual = private_statistic(ctx)
    assert actual == public_statistic(ctx, public_zero)
    assert actual["reads"] == [0, 3, 4, 5]
    assert actual["pairs"] == pairs
    assert actual["fractions"] == [336 / 799]
    assert (actual["changes"], actual["denominator"]) == (1, 3)
    assert actual["finding"] == "LIPSYNC pixels: mouth region changed between 1 of 3 sampled frame pairs"


@pytest.mark.parametrize("start,end,selected,changes,denominator", [
    (0, 5 / 30, [0, 3, 4], 2, 2),
    (3 / 30, 2, [3, 4, 5], 2, 2),
    (4 / 30, 2, [4, 5], 0, None),
    (0, 3 / 30, [0], 0, None),
])
def test_selection_window_boundaries(retained, public_zero, start, end, selected, changes, denominator):
    # Synthetic window controls over the SAME retained frame keys, not new
    # authored cuts or captures. The positive above uses the real picture cut.
    original = context(retained)
    ctx = replace(original, cut={**original.cut, "start": start, "end": end})
    actual = private_statistic(ctx)
    assert actual == public_statistic(ctx, public_zero)
    assert actual["selected"] == selected
    assert (actual["changes"], actual["denominator"]) == (changes, denominator)
    assert actual["reads"] == (selected if denominator is not None else [])
    assert (actual["finding"] is None) == (denominator is None)


def test_absent_file_is_an_error_not_zero_motion(retained, public_zero, tmp_path):
    ctx = context(retained)
    ctx.frames[3] = tmp_path / "not-a-capture.png"
    with pytest.raises(FileNotFoundError):
        private_statistic(ctx)
    with pytest.raises(FileNotFoundError):
        public_statistic(ctx, public_zero)


@pytest.mark.parametrize("selector", ["2", "foreign:presenter"])
def test_cut_selection_does_not_invent_a_reference(retained, selector):
    ctx = context(retained)
    with pytest.raises(ValueError):
        find_cut(ctx.cuts, selector)


@pytest.mark.parametrize("count", [15, 16])
def test_strict_two_percent_boundary_on_memory_only_copy(retained, public_zero, count):
    # A planted crop is a comparator control, never a relabelled frame capture.
    ctx = context(retained)
    original = ctx.frame_image(0)
    altered = original.copy()
    crop = png_crop(public_box(public_zero), ctx.frame_size)
    for i in range(count):
        point = (crop[0] + i, crop[1])
        altered.putpixel(point, (255, 255, 255) if sum(original.getpixel(point)) < 384 else (0, 0, 0))
    for box in (model.mouth_box(ctx.elements[0], 0), public_box(public_zero)):
        region = png_crop(box, ctx.frame_size)
        fraction, mask = builtin._diff_fraction(original.crop(region), altered.crop(region))
        assert mask.histogram()[255] == count
        assert fraction == count / 799
        assert (fraction > .02) == (count == 16)
