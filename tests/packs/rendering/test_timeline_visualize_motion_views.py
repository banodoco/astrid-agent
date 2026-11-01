"""Visualize views restored from the freeze: one cut definition, the motion view, layers, human output and settlement limits.

(The lint verb, the timeline-data script and the esbuild element-maths curve test live with their own areas.)"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from astrid.core.timeline.cuts import (
    cut_sample_time,
    find_cut,
    occurrences_from_bundle,
    occurrences_from_snapshot,
    picture_cuts,
)
from astrid.packs.rendering.actions.timeline_visualize import composed_frame
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import plan_filmstrip
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_execution import execute_filmstrip
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options
from astrid.packs.rendering.actions.timeline_visualize.layers import base as layer_base
from astrid.packs.rendering.actions.timeline_visualize.layers import layer_help, resolve_layers
from astrid.packs.rendering.actions.timeline_visualize.motion import lint, model
from astrid.packs.rendering.actions.timeline_visualize.motion.sheet import plan_motion_frames

FPS = 30


def _clip(cid, track, kind, at, hold, occ, **extra):
    row = {"id": f"{occ}:{cid}", "track": track, "kind": kind, "at": at, "hold": hold, "duration": hold,
           "occurrence_id": occ, "shot_occurrence_id": occ, "shot_id": occ.replace("occ-", ""),
           "shot_name": occ.upper(), "start_frame": round(at * FPS), "end_frame": round((at + hold) * FPS)}
    row.update(extra)
    return row


def layered_snapshot() -> dict:
    """Two shots, three plate cuts, five layers that enter and leave mid-cut, VO words, an sfx hit."""
    occ1, occ2 = "occ-ch01", "occ-ch02"
    clips = [
        _clip("p1", "plate", "visual", 0.0, 2.0, occ1, clipType="am-snap-plate", params={"zoom": 1}),
        _clip("pres", "sprite", "visual", 0.0, 2.0, occ1, clipType="am-presenter",
              params={"eyes": [{"x": 140, "y": 74, "w": 8}, {"x": 171, "y": 74, "w": 7}],
                      "mouth": {"x": 150, "y": 98, "w": 16}, "words": [[0.1, 0.4], [0.5, 0.9]], "seed": 3}),
        _clip("stamp", "sprite", "visual", 0.5, 1.5, occ1, clipType="am-sprite", asset=f"{occ1}:C-01",
              params={"enter": "stamp", "scale": 6, "x": 140, "y": 80}),
        _clip("p2", "plate", "visual", 2.0, 1.0, occ1, clipType="am-snap-plate", params={}),
        _clip("type", "type", "visual", 2.2, 0.8, occ1, clipType="am-type",
              params={"text": "TWO WORDS", "size": 20, "x": 1760, "y": 900, "width": 400}),
        _clip("vo", "vo", "audio", 0.0, 1.0, occ1, clipType="media",
              app={"words": [[0.1, 0.4, "It"], [0.62, 0.9, "wasn't"]]}),
        _clip("thud", "sfx", "audio", 0.5, 0.2, occ1, clipType="media", asset=f"{occ1}:sfx-00-sfx-thud"),
        _clip("p3", "plate", "visual", 3.0, 2.0, occ2, clipType="am-snap-plate", params={}),
        _clip("card", "fx", "visual", 3.5, 1.0, occ2, clipType="am-callout",
              params={"title": "DATACLAW", "body": "logs", "x": 1260, "y": 120, "width": 720}),
    ]
    return {
        "project_slug": "demo", "timeline_id": "tl", "timeline_name": "Film", "render_run_id": "run",
        "video_digest": "sha256:" + "d" * 64, "fps_rational": [FPS, 1], "duration_frames": 5 * FPS,
        "clips": clips,
        "tracks": [{"id": t, "kind": k} for t, k in (("type", "visual"), ("fx", "visual"), ("sprite", "visual"),
                                                    ("plate", "visual"), ("vo", "audio"), ("sfx", "audio"))],
        "occurrences": [
            {"occurrence_id": occ1, "shot_id": "ch01", "shot_name": "01 IT", "start": 0.0, "end": 3.0,
             "start_frame": 0, "end_frame": 90},
            {"occurrence_id": occ2, "shot_id": "ch02", "shot_name": "02 CLAW", "start": 3.0, "end": 5.0,
             "start_frame": 90, "end_frame": 150},
        ],
        "registry": {"assets": {f"{occ1}:C-01": {"resolution": "40x30"}}},
        "scripts": [], "metadata": {"selection": "composed_frame_capture"},
    }


# --- one definition of a cut --------------------------------------------------------------

def test_picture_cuts_count_bed_clips_not_layer_edges():
    snapshot = layered_snapshot()
    cuts = picture_cuts(occurrences_from_snapshot(snapshot), fps=FPS)
    assert [c["clip_id"].split(":")[-1] for c in cuts] == ["p1", "p2", "p3"]
    assert [round(c["start"], 3) for c in cuts] == [0.0, 2.0, 3.0]
    assert {s["id"].split(":")[-1] for s in cuts[0]["layers"]} == {"pres", "stamp"}
    assert find_cut(cuts, "2")["clip_id"].endswith("p2")
    assert find_cut(cuts, "p3")["index"] == 3
    assert find_cut(cuts, "@3.6")["index"] == 3
    with pytest.raises(ValueError, match="cuts 1–3"):
        find_cut(cuts, "9")


def test_bundle_and_snapshot_adapters_agree():
    shots = {
        "s1": {"payload": {"name": "01"}, "internal_timeline": {
            "tracks": [{"id": "sprite", "kind": "visual"}, {"id": "plate", "kind": "visual"}],
            "clips": [{"id": "a", "track": "plate", "at": 0.0, "hold": 1.0}, {"id": "b", "track": "plate", "at": 1.0, "hold": 1.0},
                      {"id": "x", "track": "sprite", "at": 0.2, "hold": 1.5}]}},
    }
    bundle = {"shots": shots, "placements": [{"shot_id": "s1", "occurrence_id": "o1", "placement": {"start_ms": 500},
                                              "duration_ms": 2000}]}
    cuts = picture_cuts(occurrences_from_bundle(bundle))
    assert [(c["clip_id"], c["start"], c["end"]) for c in cuts] == [("a", 0.5, 1.5), ("b", 1.5, 2.5)]


def test_tile_time_waits_for_layers_to_settle():
    cut = {"start": 0.0, "end": 2.0, "layers": [{"start": 0.0, "end": 2.0}, {"start": 1.5, "end": 2.0}]}
    # the midpoint (1.0) shows one layer; 1.5 + 0.4 shows both
    assert cut_sample_time(cut, 30) == pytest.approx(1.9)


def test_contact_view_plans_one_tile_per_picture_cut_at_review_size():
    snapshot = layered_snapshot()
    options = filmstrip_options({"view": "contact"})
    assert options["resolution"] == [480, 270]
    index = plan_filmstrip(snapshot, options)
    assert len(index["cards"]) == 3
    assert [card["cut"]["index"] for card in index["cards"]] == [1, 2, 3]
    assert all(card["sample_reasons"] == ["cut_tile"] for card in index["cards"])


def test_cut_sampling_brackets_picture_cuts_only():
    snapshot = layered_snapshot()
    index = plan_filmstrip(snapshot, filmstrip_options({"sample": "cuts"}))
    frames = [card["frame"] for card in index["cards"]]
    # boundaries at 0, 60, 90, 150 (end) -> no frame at the stamp (15) or type (66) edges
    assert 15 not in frames and 66 not in frames
    assert {59, 60, 89, 90} <= set(frames)


# --- the motion view ----------------------------------------------------------------------

def test_motion_frames_are_dense_after_events_and_bounded():
    snapshot = layered_snapshot()
    cut = picture_cuts(occurrences_from_snapshot(snapshot), fps=FPS)[0]
    elements = model.elements_from_occurrences(occurrences_from_snapshot(snapshot), snapshot["registry"]["assets"])
    frames = plan_motion_frames(cut, elements, Fraction(FPS), 150, budget=40)
    assert len(frames) <= 40
    stamp = 15
    assert {stamp - 1, stamp, stamp + 1, stamp + 2} <= set(frames)
    with pytest.raises(ValueError, match="--cut N, --range"):
        filmstrip_options({"view": "motion"})
    with pytest.raises(ValueError, match="choose one window"):
        filmstrip_options({"view": "motion", "cut": "1", "range": "0..1"})


def _frame(path: Path, t: float) -> None:
    """A synthetic frame: a box that moves right over time and a static background."""
    image = Image.new("RGB", (480, 270), (30, 40, 50))
    draw = ImageDraw.Draw(image)
    x = 40 + int(min(t, 1.0) * 200)
    draw.rectangle((x, 100, x + 40, 140), fill=(240, 120, 40))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _execute(tmp_path, snapshot, values, monkeypatch):
    class Provider:
        def __init__(self, *a, **k):
            pass

        def capture(self, cards, out_root, resolution):
            for card in cards:
                _frame(Path(out_root) / card["image"], card["time_seconds"])
            return {"evidence_source": "fixture", "frames": len(cards)}

        def close(self, force=False):
            pass

    monkeypatch.setattr(composed_frame, "RemotionFrameProvider", Provider)
    defaults = dict(
        out=tmp_path / "run", project_slug="demo", rendered_video=None, filmstrip_authority=None,
        timeline=tmp_path / "timeline.json", assets_registry=tmp_path / "assets.json",
        materialized_root=None, materialized_objects=None, timeline_slug=None, render_run=None,
        include_media=False, range_value=None, at=None, frame=None, every=None, every_frames=None,
        sample=None, include_cuts=False, columns=None, page_size=None, resolution=None, shot=None,
        clip=None, occurrence=None, asset=None, context=3.0, neighbors=0, show=None, hide=None,
        track=None, detail=False, formats=None, view="filmstrip", cut=None, layers=None, beats=None,
        frame_budget=None,
    )
    defaults.update(values)
    authority = {"mode": "composed_capture", "capture_snapshot": snapshot, "capture_identity": "sha256:" + "d" * 64}
    return execute_filmstrip(SimpleNamespace(**defaults), authority=authority)


def test_motion_view_writes_two_pages_findings_and_timing(tmp_path, monkeypatch):
    beats = json.dumps({"beats": [0.5, 1.0], "downbeats": [0.0], "hits": [{"t": 0.52, "kind": "stab"}]})
    result = _execute(tmp_path, layered_snapshot(), {"view": "motion", "cut": "1", "beats": beats}, monkeypatch)
    outputs = result["outputs"]
    names = [Path(page).name for page in outputs["pages"]]
    assert names == ["motion-cut-01.png", "motion-cut-01-frames.png"]
    assert outputs["primary_page"].endswith("motion-cut-01.png")
    pack = Path(outputs["pack_root"])
    timing = json.loads((pack / "timing.json").read_text())
    assert timing["view"] == "motion" and timing["resolution"] == [480, 270] and timing["frames"] <= 60
    findings = (pack / "findings.txt").read_text().splitlines()
    assert findings[0].startswith("MOTION cut 1 ")
    assert any(line.startswith("LIPSYNC") for line in findings)
    assert any(line.startswith("STILL") for line in findings)
    assert any(line.startswith("TIME") and "C-01" in line for line in findings)
    with Image.open(outputs["primary_page"]) as page:
        assert page.size[0] == 1600 and page.size[1] < 2400


def test_contact_view_draws_bounds_when_asked(tmp_path, monkeypatch):
    result = _execute(tmp_path, layered_snapshot(), {"view": "contact", "layers": "bounds"}, monkeypatch)
    pages = result["outputs"]["pages"]
    assert [Path(p).name for p in pages] == ["contact-sheet.png"]
    assert json.loads((Path(result["outputs"]["pack_root"]) / "timing.json").read_text())["frames"] == 3


# --- layers registry ----------------------------------------------------------------------

def test_layer_registry_lists_builtins_and_pack_layers(tmp_path):
    names = {layer.name for layer in resolve_layers(None, view="motion")}
    assert {"sync", "curves", "still", "diff", "lipsync", "lint", "strip", "onion"} <= names
    assert "bounds" not in names and "heat" not in names  # opt-in / experimental
    assert [layer.name for layer in resolve_layers(["bounds"], view="contact")] == ["bounds"]
    with pytest.raises(ValueError, match="known layers"):
        resolve_layers(["nope"], view="motion")
    text = layer_help()
    assert "rhythm" in text and "editorial pack" in text  # contributed by astrid/packs/editorial/visualize_layers


def test_a_pack_layer_module_is_discovered(tmp_path, monkeypatch):
    pack = tmp_path / "packs" / "mypack" / "visualize_layers"
    pack.mkdir(parents=True)
    (pack / "tempo.py").write_text(
        "from astrid.packs.rendering.actions.timeline_visualize.layers import Layer, LayerResult\n"
        "LAYER = Layer('tempo', 'cuts per minute', lambda ctx: LayerResult(None, ['TEMPO ok']))\n"
    )
    (pack / "broken.py").write_text("raise RuntimeError('boom')\n")
    found = layer_base.discover(packs_root=tmp_path / "packs", refresh=True)
    assert found["tempo"].source.startswith("mypack pack")
    assert "boom" in layer_help()


# --- lint ---------------------------------------------------------------------------------

def test_lint_flags_face_frame_small_and_sync():
    snapshot = layered_snapshot()
    occurrences = occurrences_from_snapshot(snapshot)
    elements = model.elements_from_occurrences(occurrences, snapshot["registry"]["assets"])
    cuts = picture_cuts(occurrences, fps=FPS)
    results = {cut["index"]: [f.code for f in findings] for cut, findings in lint.lint_cuts(cuts, elements, FPS)}
    assert "FACE" in results[1]       # the stamp sits on the presenter's face box
    assert "SMALL" in results[2] and "SAFE" in results[2]  # 20 px type at x=1760 runs past x=1824
    assert "FRAME" in results[3]      # a 720 px card at x=1260 runs off the right edge
    texts = [f.message for _cut, fs in lint.lint_cuts(cuts, elements, FPS) for f in fs if f.code == "FRAME"]
    assert "60 px past the frame edge" in texts[0]


def test_declared_face_zone_on_real_footage_is_linted():
    """A real-footage slot declares params.faceZone; an overlay on it is a FACE finding, and the
    footage itself (a full-frame plate) never counts as covering anything."""
    snapshot = layered_snapshot()
    occ = "occ-ch02"
    snapshot["clips"] = [c for c in snapshot["clips"] if c["occurrence_id"] != occ] + [
        _clip("foot", "plate", "visual", 3.0, 2.0, occ, clipType="am-footage", asset=f"{occ}:slot-A1",
              params={"slot": "A1", "faceZone": {"x": 700, "y": 110, "w": 500, "h": 610}}),
        _clip("claw", "sprite", "visual", 3.2, 1.0, occ, clipType="am-sprite", asset=f"{occ}:D-06",
              params={"scale": 6, "x": 130, "y": 40}),
        _clip("side", "fx", "visual", 3.2, 1.0, occ, clipType="am-callout",
              params={"title": "SIDE", "x": 1300, "y": 160, "width": 400, "titleSize": 56}),
    ]
    snapshot["registry"]["assets"][f"{occ}:D-06"] = {"resolution": "87x140"}
    occurrences = occurrences_from_snapshot(snapshot)
    elements = model.elements_from_occurrences(occurrences, snapshot["registry"]["assets"])
    foot = next(e for e in elements if e.type == "am-footage")
    boxes = model.boxes_at(foot, 3.5, FPS)
    assert [b.kind for b in boxes] == ["plate", "face"]
    assert boxes[1].rect == (700, 110, 1200, 720)
    cuts = picture_cuts(occurrences, fps=FPS)
    findings = [f for cut, fs in lint.lint_cuts(cuts, elements, FPS) if cut["index"] == 3 for f in fs]
    face = [f.message for f in findings if f.code == "FACE"]
    assert len(face) == 1 and "D-06" in face[0]   # the claw sits on the declared face; the side card does not


def test_callout_box_reads_its_type_sizes():
    big = model.Element("c", "am-callout", "fx", 0, 1, {"title": "TESTERS WANTED", "body": "any machine",
                                                        "x": 100, "y": 100, "width": 700, "titleSize": 64, "bodySize": 36}, {})
    small = model.Element("c", "am-callout", "fx", 0, 1, {"title": "TESTERS WANTED", "body": "any machine",
                                                          "x": 100, "y": 100, "width": 700}, {})
    (b,), (s,) = model.boxes_at(big, 0.5, FPS), model.boxes_at(small, 0.5, FPS)
    assert b.text_px == 36 and s.text_px == 24
    assert b.rect[3] > s.rect[3]


def test_presenter_face_box_follows_zoom_and_focus():
    element = model.Element("p", "am-presenter", "sprite", 0, 1, {"eyes": [{"x": 140, "y": 74, "w": 8}],
                            "mouth": {"x": 150, "y": 98, "w": 16}, "zoom": 2, "focus": {"x": 158, "y": 100}}, {})
    wide = model.face_box(model.Element("p", "am-presenter", "sprite", 0, 1, {**element.params, "zoom": 1}, {}))
    close = model.face_box(element)
    assert close[2] - close[0] == pytest.approx(2 * (wide[2] - wide[0]))


def test_mouth_state_matches_presenter_core_cycle():
    element = model.Element("p", "am-presenter", "sprite", 0, 1, {"words": [[0.0, 0.2]], "seed": 7}, {})
    states = [model.mouth_state(element, frame, 30) for frame in range(0, 12)]
    assert set(states[:6]) == {"closed", "half", "open"}
    assert states[-1] == "closed"
    # same integers as hashUnit in astrid_motion/elements/_shared/am.tsx (checked under Node 20)
    assert [round(model.hash_unit(s, i), 9) for s, i in ((7, 0), (7, 1), (20, 5), (2, 1000))] == [
        0.963741059, 0.675856872, 0.472920862, 0.114910463]


# --- the CLI: human-first visualize, lint, pages ------------------------------------------

def test_visualize_prints_page_counts_timing_and_next_commands():
    from argparse import Namespace

    from astrid.packs.timeline import cli

    outputs = {
        "pages": ["/x/contact-sheet.png"], "primary_page": "/x/contact-sheet.png",
        "timing": {"frames": 30, "resolution": [480, 270], "wall_s": 61.2, "queued_s": 3.0, "capture_s": 48.0, "compose_s": 6.0},
    }
    text = cli._visualization_summary(outputs, parsed=Namespace(project="demo", beats=None),
                                      inputs={"view": "contact", "timeline_slug": "T1"})
    lines = text.splitlines()
    assert lines[0] == "contact sheet: /x/contact-sheet.png"
    assert "30 tiles, one per picture cut" in lines[1] and "480x270" in lines[1]
    assert "wall 61 s = queued 3 s + capture 48 s + compose 6 s" in text
    assert "--view motion --cut N" in text and "timelines lint T1" in text and "timelines show T1" in text
    parser_args = cli.build_parser(object()).parse_args(["visualize", "T1", "--project", "demo"])
    assert parser_args.json is False


def test_sdk_pages_put_the_primary_page_first_and_read_timing(tmp_path):
    from astrid.sdk.invocation import _filmstrip_pages, _filmstrip_sidecars

    for name in ("contact-sheet.png", "filmstrip-001.png", "input-band-001.png", "motion-cut-04.png"):
        (tmp_path / name).write_bytes(b"png")
    (tmp_path / "timing.json").write_text(json.dumps({"capture_s": 4.5, "frames": 30}))
    (tmp_path / "findings.txt").write_text("MOTION cut 4\nSTILL x\n")
    pages = [Path(p).name for p in _filmstrip_pages(tmp_path, {"entrypoints": {"png": "motion-cut-04.png"}})]
    assert pages == ["motion-cut-04.png", "contact-sheet.png", "filmstrip-001.png"]
    sidecars = _filmstrip_sidecars(tmp_path)
    assert sidecars["timing"]["frames"] == 30 and sidecars["findings"] == ["MOTION cut 4", "STILL x"]


def _wav(path: Path, seconds: float, *, loud: list[tuple[float, float]], rate: int = 8000) -> None:
    """A mono 16-bit tone that sounds only inside ``loud`` windows."""
    import math
    import struct
    import wave

    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            t = i / rate
            on = any(a <= t < b for a, b in loud)
            frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * 220 * t)) if on else 0)
        out.writeframes(bytes(frames))


def _with_music(snapshot: dict) -> dict:
    occ = "occ-ch01"
    snapshot["clips"].append(_clip("music", "music", "audio", 0.0, 3.0, occ, clipType="media", asset=f"{occ}:music-cue",
                                   **{"from": 10.0, "to": 13.0},
                                   app={"beats": {"source": "cue.beats.json", "beats": [10.5, 11.0, 11.5],
                                                  "downbeats": [10.5], "hits": [[10.52, "stab"]]}}))
    for clip in snapshot["clips"]:
        if clip["id"].endswith(":vo"):
            clip["asset"] = f"{occ}:vo-s01"
    snapshot["tracks"].append({"id": "music", "kind": "audio"})
    return snapshot


def test_beats_travel_on_the_music_clip_and_flags_override():
    snapshot = _with_music(layered_snapshot())
    elements = model.elements_from_occurrences(occurrences_from_snapshot(snapshot))
    beats = model.timeline_beats(None, elements, 0.0, 3.0)
    # cue second 10.5 plays at timeline 0.5 (clip at 0.0 plays the cue from 10.0)
    assert beats["beats"] == [0.5, 1.0, 1.5] and beats["hits"] == [(0.52, "stab")]
    assert beats["source"] == ["cue.beats.json"]
    override = model.timeline_beats({"beats": [10.25]}, elements, 0.0, 3.0)
    assert override["beats"] == [0.25] and override["source"] == ["--beats"]


def test_audio_envelope_finds_vo_gaps_and_dead_air(tmp_path):
    from astrid.packs.rendering.actions.timeline_visualize.motion import audio

    snapshot = _with_music(layered_snapshot())
    elements = model.elements_from_occurrences(occurrences_from_snapshot(snapshot))
    _wav(tmp_path / "vo.wav", 1.0, loud=[(0.1, 0.4)])
    _wav(tmp_path / "music.wav", 14.0, loud=[(10.0, 10.6), (11.5, 13.0)])
    files = {"occ-ch01:vo-s01": tmp_path / "vo.wav", "occ-ch01:music-cue": tmp_path / "music.wav"}
    env = audio.envelope(elements, files, 0.0, 3.0, bins=300)
    assert env.step >= audio.MIN_BIN_S
    assert any(a <= 0.45 and b >= 1.4 for a, b in env.silences("vo"))
    dead = env.dead_air()
    assert dead and 0.55 <= dead[0][0] <= 0.7 and 1.4 <= dead[0][1] <= 1.6  # music muted 10.6–11.5 cue seconds
    assert env.level_db("music", 0.0, 0.5) > -25


def test_contact_and_motion_draw_audio_and_preview(tmp_path, monkeypatch):
    snapshot = _with_music(layered_snapshot())
    _wav(tmp_path / "vo.wav", 1.0, loud=[(0.1, 0.4)])
    _wav(tmp_path / "music.wav", 14.0, loud=[(10.0, 10.6), (11.5, 13.0)])
    registry = tmp_path / "assets.json"
    registry.write_text(json.dumps({"assets": {
        "occ-ch01:vo-s01": {"file": str(tmp_path / "vo.wav")},
        "occ-ch01:music-cue": {"file": str(tmp_path / "music.wav")},
    }}))
    contact = _execute(tmp_path / "c", snapshot, {"view": "contact", "assets_registry": registry}, monkeypatch)
    lines = contact["outputs"]["findings"]
    assert any(line.startswith("DEADAIR") for line in lines) and any(line.startswith("NO-VO") for line in lines)
    motion = _execute(tmp_path / "m", snapshot, {"view": "motion", "cut": "1", "preview": True,
                                                 "assets_registry": registry}, monkeypatch)
    outputs = motion["outputs"]
    assert any(line.startswith("AUDIO") and "dead air" in line for line in outputs["findings"])
    assert any("vs music" in line for line in outputs["findings"])  # beats from the music clip
    gif = Path(outputs["pack_root"]) / "motion-cut-01.gif"
    with Image.open(gif) as preview:
        assert preview.size[0] <= 480 and preview.n_frames >= 10
    from astrid.sdk.invocation import _filmstrip_sidecars

    assert _filmstrip_sidecars(Path(outputs["pack_root"]))["preview"].endswith("motion-cut-01.gif")
    with pytest.raises(ValueError, match="--view motion"):
        filmstrip_options({"view": "contact", "preview": True})


# --- sequences: many stepped clips, one picture cut --------------------------------------

def _sequence_snapshot(steps: int = 6) -> dict:
    snapshot = layered_snapshot()
    occ = "occ-ch02"
    snapshot["clips"] = [c for c in snapshot["clips"] if not c["id"].endswith(":p3")]
    for k in range(steps):
        snapshot["clips"].append(_clip(f"s{k:02d}", "plate", "visual", 3.0 + k * 2.0 / steps, 2.0 / steps, occ,
                                       clipType="am-snap-plate", params={}, app={"sequence": "c20-seq0", "sequence_index": k}))
    return snapshot


def test_a_sequence_of_stepped_clips_is_one_cut_everywhere():
    snapshot = _sequence_snapshot()
    cuts = picture_cuts(occurrences_from_snapshot(snapshot), fps=FPS)
    assert len(cuts) == 3
    seq = cuts[2]
    assert seq["sequence"]["steps"] == 6 and seq["sequence"]["id"] == "c20-seq0"
    assert (seq["start"], seq["end"]) == (3.0, 5.0)
    assert all(not layer["clip"].get("app", {}).get("sequence") for layer in seq["layers"])
    assert find_cut(cuts, "s03")["index"] == 3  # any step's clip id finds the cut
    # lint: the steps are the picture, not six entrances to sync
    elements = model.elements_from_occurrences(occurrences_from_snapshot(snapshot))
    findings = dict((cut["index"], fs) for cut, fs in lint.lint_cuts(cuts, elements, FPS))
    assert not [f for f in findings[3] if f.code in ("SYNC", "BEAT")]


def test_contact_shows_a_sequence_as_one_tile_with_steps(tmp_path, monkeypatch):
    result = _execute(tmp_path, _sequence_snapshot(), {"view": "contact"}, monkeypatch)
    index = json.loads((Path(result["outputs"]["pack_root"]) / "timing.json").read_text())
    assert index["tiles"] == 3 and index["frames"] == 5  # 3 tiles + first/last step of the sequence
    motion = _execute(tmp_path / "m", _sequence_snapshot(steps=30), {"view": "motion", "cut": "3"}, monkeypatch)
    lines = motion["outputs"]["findings"]
    assert lines[0].startswith("MOTION cut 3 ")
    assert any("sequence c20-seq0: 30 steps" in line for line in lines)
    assert json.loads((Path(motion["outputs"]["pack_root"]) / "timing.json").read_text())["frames"] <= 60


def test_show_marks_a_sequence_row():
    from astrid.sdk.timeline_cuts import build_cut_table, render_cut_table

    tracks = [{"id": "plate", "kind": "visual"}]
    clips = [{"id": f"c20-{k:02d}", "track": "plate", "clipType": "am-snap-plate", "at": k * 0.2, "hold": 0.2,
              "app": {"sequence": "c20-seq0", "sequence_index": k}} for k in range(5)]
    clips.append({"id": "c21", "track": "plate", "clipType": "am-snap-plate", "at": 1.0, "hold": 1.0})
    bundle = {"shots": {"s": {"payload": {"name": "05"}, "internal_timeline": {"tracks": tracks, "clips": clips}}},
              "placements": [{"shot_id": "s", "occurrence_id": "o", "placement": {"start_ms": 0}, "duration_ms": 2000}]}
    table = build_cut_table(bundle)
    assert [row["clip_id"] for row in table["rows"]] == ["c20-00", "c21"]
    assert "sequence ×5 steps" in render_cut_table(table, table["rows"], title="t")


# --- windows and presets: one fast capture path, one page ---------------------------------

def test_window_presets_sample_and_fit_one_page(tmp_path, monkeypatch):
    snapshot = layered_snapshot()
    scan = _execute(tmp_path / "scan", snapshot, {"view": "motion", "preset": "scan", "range_value": "0..4"}, monkeypatch)
    timing = json.loads((Path(scan["outputs"]["pack_root"]) / "timing.json").read_text())
    assert timing["frames"] == 9 and timing["resolution"] == [480, 270]  # every 0.5 s over 4 s, plus the end
    assert [Path(p).name for p in scan["outputs"]["pages"]] == ["view-scan-0.00-4.00.png"]
    window = json.loads((Path(scan["outputs"]["pack_root"]) / "window.json").read_text())
    assert window["preset"] == "scan" and window["later"] == [4.0, 5.0] and window["earlier"] is None
    assert window["cut"] == 2
    motion = _execute(tmp_path / "motion", snapshot, {"view": "motion", "preset": "motion", "range_value": "0..2"}, monkeypatch)
    assert json.loads((Path(motion["outputs"]["pack_root"]) / "timing.json").read_text())["resolution"] == [640, 360]
    with Image.open(motion["outputs"]["primary_page"]) as page:
        assert page.size[0] >= 3 * 640  # big frames, 3 per row
    frame = _execute(tmp_path / "frame", snapshot, {"view": "motion", "preset": "frame", "at": "1.5"}, monkeypatch)
    assert json.loads((Path(frame["outputs"]["pack_root"]) / "timing.json").read_text())["frames"] == 1
    beat = _execute(tmp_path / "beat", snapshot, {"view": "motion", "preset": "beat", "range_value": "0..1"}, monkeypatch)
    labels = json.loads((Path(beat["outputs"]["pack_root"]) / "frame-index.json").read_text())
    assert labels  # frames at "It", "wasn't", the thud and the stamp
    with pytest.raises(ValueError, match="at most 3 s"):
        filmstrip_options({"view": "motion", "preset": "motion", "range": "0..5"})  # options parse; window check below
        _execute(tmp_path / "long", snapshot, {"view": "motion", "preset": "motion", "range_value": "0..5"}, monkeypatch)


def test_cli_window_navigation_offers_neighbours():
    from astrid.packs.timeline import cli

    window = {"preset": "scan", "start": 5.0, "end": 10.0, "earlier": [0.0, 5.0], "later": [10.0, 15.0],
              "zoom_in": [6.5, 9.0], "zoom_out": [2.5, 12.5], "busiest": 7.75, "cut": 3, "cuts_in": [2, 3]}
    rows = dict((name, " ".join(argv)) for name, argv in cli._window_navigation(window, ["visualize"]))
    assert rows["earlier 0.00–5.00 s"].endswith("--preset scan --range 0.00..5.00")
    assert rows["later 10.00–15.00 s"].endswith("--range 10.00..15.00")
    assert rows["zoom in on the busiest moment 7.75 s"].endswith("--preset motion --range 6.50..9.00")
    assert rows["zoom out"].endswith("--preset scan --range 2.50..12.50")
    assert rows["same window as beat"].endswith("--preset beat --range 5.00..10.00")
    assert rows["the cut it is in (#3)"].endswith("--cut 3")


# --- the platform: pack layers + checks, project rules, data tracks, diff ----------------

GUIDE_MODULE = '''
from PIL import ImageDraw
from astrid.packs.rendering.actions.timeline_visualize.layers import Check, Layer, LayerResult
from astrid.packs.rendering.actions.timeline_visualize.layers.base import PALETTE

def render(ctx):
    image = ctx.panel(60, title="cut length")
    ImageDraw.Draw(image).rectangle((ctx.x_of(float(ctx.cut["start"])), 30, ctx.x_of(float(ctx.cut["end"])), 50),
                                    fill=PALETTE["key"])
    return LayerResult(image, [f"LENGTH cut {ctx.cut['index']} is {float(ctx.cut['duration']):.2f} s"])

def run(ctx):
    limit = ctx.param("max_words_on_screen")
    return [ctx.finding("WORDY", f"{e.label} shows {len(str(e.params.get('text')).split())} words", t=e.start,
                        fix={"clip": e.short_id, "set": {"params.text": "..."}})
            for e in ctx.elements if e.type == "am-type" and len(str(e.params.get("text") or "").split()) > limit]

LAYER = Layer("cutlength", "the cut's span", render, needs=("doc",))
CHECK = Check("wordy-type", "type longer than max_words_on_screen words", run,
              params={"max_words_on_screen": 1}, scope="cut", codes=("WORDY",))
'''


def test_the_guide_examples_register_and_run(tmp_path):
    from astrid.packs.rendering.actions.timeline_visualize.layers import base as layer_base
    from astrid.packs.rendering.actions.timeline_visualize.motion.conditions import run_checks

    folder = tmp_path / "packs" / "mypack" / "visualize_layers"
    folder.mkdir(parents=True)
    (folder / "cut_length.py").write_text(GUIDE_MODULE)
    layer_base.discover(packs_root=tmp_path / "packs", refresh=True)
    assert "cutlength" in layer_base.layer_help() and "wordy-type" in layer_base.check_help()
    snapshot = layered_snapshot()
    occurrences = occurrences_from_snapshot(snapshot)
    elements = model.elements_from_occurrences(occurrences)
    cuts = picture_cuts(occurrences, fps=FPS)
    per_cut, _timeline = run_checks(cuts, elements, FPS)
    wordy = [f for _c, fs in per_cut for f in fs if f.code == "WORDY"]
    assert wordy and wordy[0].check == "wordy-type" and wordy[0].fix["set"] == {"params.text": "..."}
    per_cut, _ = run_checks(cuts, elements, FPS, params={"max_words_on_screen": 5})
    assert not [f for _c, fs in per_cut for f in fs if f.code == "WORDY"]


def test_project_rules_set_thresholds_and_severities(tmp_path):
    from astrid.packs.rendering.actions.timeline_visualize.motion.conditions import run_checks
    from astrid.packs.rendering.actions.timeline_visualize.motion.rules import find_rules, load_rules

    rules_file = tmp_path / "astrid-lint.toml"
    rules_file.write_text('max_cut_s = 1.5\nmax_presenter_share = 0.2\n[severity]\nLONG = "error"\nEDGE = "off"\n')
    (tmp_path / "sub").mkdir()
    assert find_rules(tmp_path / "sub") == rules_file
    rules = load_rules(rules_file)
    snapshot = layered_snapshot()
    occurrences = occurrences_from_snapshot(snapshot)
    elements = model.elements_from_occurrences(occurrences)
    cuts = picture_cuts(occurrences, fps=FPS)
    per_cut, timeline = run_checks(cuts, elements, FPS, params=rules["params"], severity=rules["severity"])
    longs = [f for _c, fs in per_cut for f in fs if f.code == "LONG"]
    assert longs and all(f.severity == "error" for f in longs)  # cuts 1 and 3 are 2.0 s
    assert any(f.code == "SHARE" for f in timeline)  # presenter on 2 of 5 s
    bad = tmp_path / "bad.toml"
    bad.write_text("max_cut = 4\n")
    with pytest.raises(ValueError, match="unknown key 'max_cut'.*max_cut_s"):
        load_rules(bad)


def test_data_tracks_of_every_kind_read_in_timeline_seconds(tmp_path):
    from astrid.packs.rendering.actions.timeline_visualize.motion import data

    snapshot = layered_snapshot()
    stamp = next(c for c in snapshot["clips"] if c["id"].endswith(":stamp"))
    data.attach(stamp, "claw", data.make_track("points", [[0.1, "close"], [0.5, "open"]], units="s"))
    data.attach(stamp, "hold", data.make_track("intervals", [[0.0, 0.3, "still"]]))
    data.attach(stamp, "energy", data.make_track("series", {"t0": 0.0, "step": 0.5, "values": [1, 2, 3]}, units="x"))
    data.attach(stamp, "face", data.make_track("boxes", [[0.2, 700, 100, 300, 300, "face"]], units="px"))
    with pytest.raises(ValueError, match="kind must be"):
        data.make_track("blobs", [])
    elements = model.elements_from_occurrences(occurrences_from_snapshot(snapshot))
    found = {t.name: t for t in data.tracks(elements) if not t.derived}
    assert found["claw"].points == [(0.6, "close"), (1.0, "open")]  # the stamp clip starts at 0.5
    assert found["hold"].intervals == [(0.5, 0.8, "still")]
    assert [t for t, _v in found["energy"].series] == [0.5, 1.0, 1.5]
    assert found["face"].boxes_at(0.7) == [(700, 100, 1000, 400)]
    derived = {t.name for t in data.tracks(elements) if t.derived}
    assert {"words", "presenter_words"} <= derived


def test_diff_view_scope_condition():
    from astrid.packs.rendering.actions.timeline_visualize.motion.diffview import compare

    def bundle(x):
        clips = [{"id": "p1", "track": "plate", "at": 0.0, "hold": 2.0}, {"id": "p2", "track": "plate", "at": 2.0, "hold": 2.0},
                 {"id": "t2", "track": "type", "clipType": "am-type", "at": 2.2, "hold": 1.0, "params": {"x": x}}]
        tracks = [{"id": "type", "kind": "visual"}, {"id": "plate", "kind": "visual"}]
        return {"shots": {"s": {"internal_timeline": {"tracks": tracks, "clips": clips}}},
                "placements": [{"shot_id": "s", "occurrence_id": "o", "placement": {"start_ms": 0}, "duration_ms": 4000}]}

    report = compare(bundle(100), bundle(140), edited=[1])
    scope = [f for f in report["findings"] if f.code == "SCOPE"]
    assert report["changed_after"] == [2] and scope and "params.x 100→140" in scope[0].message
    assert not [f for f in compare(bundle(100), bundle(140), edited=[2])["findings"] if f.code == "SCOPE"]


# --- element motion: the element's own maths, the model as fallback ---------------------
from astrid.packs.rendering.actions.timeline_visualize.motion import element_motion  # noqa: E402

V6_PUSHED = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "v6_pushed_clips.json").read_text())


def _v6_elements():
    elements = [model.Element(c["id"], c["clipType"], c["track"], c["at"], c["at"] + c["hold"], c["params"], c)
                for c in V6_PUSHED["clips"]]
    maths = element_motion.ElementMaths(elements)
    for element in elements:
        element.maths = maths
    return {e.id: e for e in elements}, maths


def _own(maths, element):
    values = maths.at(element, 0, V6_PUSHED["fps"])
    assert values is not None, f"element maths unavailable: {maths.note}"
    return values


def test_elements_ship_their_own_motion():
    found = element_motion.modules()
    assert {"am-presenter", "am-snap-plate"} <= set(found)
    assert all(path.name == "motion.ts" and (path.parent / "element.yaml").is_file() for path in found.values())


@pytest.mark.remotion
def test_a_real_v6_push_comes_from_the_element():
    """c16 (48.53 s): plate and presenter push 1 -> 1.35 over 100 frames; c07: zoom 3 -> 1 from frame 98."""
    by_id, maths = _v6_elements()
    _own(maths, by_id["c16-00-am-snap-plate"])
    fps = V6_PUSHED["fps"]
    for clip in ("c16-00-am-snap-plate", "c16-01-am-presenter"):
        element = by_id[clip]
        zooms = [model.own_motion(element, f, fps)["zoom"] for f in (0, 50, 100)]
        assert zooms == pytest.approx([1.0, 1.175, 1.35])
        assert model.props_at(element, element.start + 50 / fps + 1e-6, fps)["zoom"] == pytest.approx(1.175)
    plate = by_id["c07-00-am-snap-plate"]
    assert [model.own_motion(plate, f, fps)["zoom"] for f in (0, 98, 110, 138, 200)] == pytest.approx(
        [3.0, 3.0, 2.4, 1.0, 1.0])
    # plate and overlay stay registered through the push: the same view on every frame
    for f in range(0, 101, 5):
        a = model.own_motion(by_id["c16-00-am-snap-plate"], f, fps)
        b = model.own_motion(by_id["c16-01-am-presenter"], f, fps)
        assert (a["zoom"], a["pan_x"], a["pan_y"]) == (b["zoom"], b["pan_x"], b["pan_y"])
    # the face box follows the element's view, so it grows with the push
    early, late = model.face_box(by_id["c16-01-am-presenter"], 0), model.face_box(by_id["c16-01-am-presenter"], 100)
    assert (late[2] - late[0]) == pytest.approx(1.35 * (early[2] - early[0]), rel=0.02)


def test_without_node_the_mirror_answers_and_says_so(monkeypatch):
    monkeypatch.setenv("ASTRID_NODE_EXECUTABLE", "/nonexistent/node")
    monkeypatch.setattr(element_motion.shutil, "which", lambda _name: None)
    by_id, maths = _v6_elements()
    plate = by_id["c16-00-am-snap-plate"]
    assert maths.at(plate, 50, 30) is None and "no Node" in maths.note
    assert model.props_at(plate, plate.start + 50 / 30 + 1e-6, 30)["zoom"] == pytest.approx(1.175)


@pytest.mark.remotion
def test_the_fallback_mirror_still_matches_the_element_on_v6_pushes():
    """If presenter-core.ts changes, this names the drift in motion/model.py (the fallback only)."""
    by_id, maths = _v6_elements()
    _own(maths, by_id["c07-00-am-snap-plate"])
    for element in by_id.values():
        frames = round((element.end - element.start) * 30)
        for f in range(frames):
            own = model.own_motion(element, f, 30)
            mirror = model.PROPS[element.type](model.Element(element.id, element.type, element.track, element.start,
                                                             element.end, {**element.params, "bob": 0}, {}), f, 30)
            assert own["zoom"] == pytest.approx(mirror["zoom"]), (element.id, f)
            if "pan_x" in mirror:
                assert own["pan_x"] == pytest.approx(mirror["pan_x"]), (element.id, f)
