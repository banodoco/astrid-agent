"""``timelines show``: complete cut table, paging, script word lines, --at, --clips, working copy. Offline."""
from __future__ import annotations

import argparse
import copy
import json
import re
from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_cuts import build_cut_table, decimal_seconds, page_rows
from astrid.sdk.timeline_views import render_at, render_asset_provenance, render_clips, render_script
from astrid.packs.timeline import cli

FPS = 30


def _shot(clips):
    return {
        "payload": {"name": "SHOT"},
        "internal_timeline": {
            "tracks": [
                {"id": "sprite", "kind": "visual"}, {"id": "plate", "kind": "visual"},
                {"id": "vo", "kind": "audio"}, {"id": "music", "kind": "audio"},
            ],
            "clips": clips,
            "registry": {"assets": {"P": {"media_id": "m-p"}, "R": {"media_id": "m-r"}, "Q": {"media_id": "m-q"}}},
        },
    }


def bundle():
    """Shot A (0–4 s): one plate cut, a rocket layer, VO "it went viral". Shot B (4–8 s): a plate and VO "live now"."""
    a = [
        {"id": "a-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "a-rocket", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 1.0, "hold": 1.0,
         "params": {"x": 10}, "app": {"anchor": {"word": "viral", "text": "viral", "offset_s": 0.1}}},
        {"id": "a-vo", "clipType": "media", "track": "vo", "asset": "P", "at": 0.5, "from": 0.0, "to": 2.0,
         "app": {"segment": "s1", "words": [[0.1, 0.3, "it"], [0.4, 0.6, "went"], [0.8, 1.2, "viral"]]}},
        {"id": "a-music", "clipType": "media", "track": "music", "asset": "Q", "at": 0.0, "from": 0.0, "to": 4.0},
    ]
    b = [
        {"id": "b-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "Q", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "b-vo", "clipType": "media", "track": "vo", "asset": "P", "at": 1.9, "from": 0.0, "to": 1.0,
         "app": {"segment": "s2", "words": [[0.1, 0.4, "Live"], [0.5, 0.8, "now"]]}},
    ]
    return {
        "project_id": "p", "timeline_id": "t",
        "base_parent": {"revision_id": "rev-0"},
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": FPS}}}}, "clips": []},
        "placements": [
            {"shot_id": "A", "occurrence_id": "occ-A", "placement": {"start_ms": 0}, "duration_ms": 4000},
            {"shot_id": "B", "occurrence_id": "occ-B", "placement": {"start_ms": 4000}, "duration_ms": 4000},
        ],
        "shots": {"A": _shot(a), "B": _shot(b)},
    }


def many_cuts(count=60):
    """One shot with ``count`` picture cuts, one second each (more than the old 50-row projection)."""
    clips = [
        {"id": f"c{i:02d}-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P",
         "at": float(i), "hold": 1.0, "params": {}}
        for i in range(count)
    ]
    return {
        "project_id": "p", "timeline_id": "t",
        "base_parent": {"revision_id": "rev-0"},
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": FPS}}}}, "clips": []},
        "placements": [{"shot_id": "M", "occurrence_id": "occ-M", "placement": {"start_ms": 0}, "duration_ms": count * 1000}],
        "shots": {"M": _shot(clips)},
    }


@pytest.fixture(autouse=True)
def no_working_copy(monkeypatch):
    """Tests read the bundle they pass in; no runtime, so no working copy unless a test sets one."""
    import astrid.sdk.timeline_checkout as checkout_module

    monkeypatch.setattr(checkout_module, "find_draft", lambda project, timeline, name="main", **kw: None)


def ns(**kw):
    base = dict(project="p", ref="t", json=False, published=False, revision_id=None, at=None, clips=False,
                shot=None, summary=False, range=None, as_view="cuts", page=None, page_size=None, layers=False)
    base.update(kw)
    return argparse.Namespace(**base)


def opener(doc):
    def open_bundle(project, ref, revision_id=None):
        return DomainResult.success({
            "bundle": copy.deepcopy(doc), "project_id": "p", "timeline_id": "t",
            "revision_id": "rev-0", "is_current_head": True, "head_revision_id": "rev-0",
        })
    return open_bundle


def run(capsys, doc, **kw):
    code = cli._print_cut_table(ns(**kw), opener(doc))
    out = capsys.readouterr().out
    return code, out


def test_cut_table_json_is_complete_and_has_no_rationals(capsys):
    doc = many_cuts(60)
    code, out = run(capsys, doc, json=True)
    assert code == 0
    data = json.loads(out)["data"]
    assert len(data["rows"]) == 60, "every cut, never the 50-row projection"
    assert data["counts"]["cuts"] == 60
    assert data["working_copy"] is None
    assert not re.search(r"\[\s*\d+,\s*\d+\s*\]", out), "no raw rational pairs in show --json"


def test_address_view_can_print_saved_sprite_provenance(capsys):
    doc = bundle()
    doc["shots"]["A"]["internal_timeline"]["clips"][1]["app"] = {"cut": "c01", "layer": "rocket"}
    doc["shots"]["A"]["internal_timeline"]["registry"]["assets"]["R"].update({
        "handle": "run:SPRITES/sprites#0",
        "pixel_provenance": {"executor": "pixel.sprites", "run_id": "SPRITES",
                             "lattice": {"detected": True, "block": 3.5},
                             "outline_integrity": {"fraction": 0.70}},
    })
    line = render_asset_provenance(Checkout(copy.deepcopy(doc)).clip("c01.rocket"))
    assert line == ("sprite provenance c01.rocket: pixel.sprites → run SPRITES → "
                    "run:SPRITES/sprites#0 → lattice 3.5 px/block → outline integrity 0.70")
    code, out = run(capsys, doc, range="c01.rocket")
    assert code == 0
    assert line in out


def test_show_cut_id_range_is_reused_by_the_script_view(capsys):
    doc = many_cuts(12)
    clips = doc["shots"]["M"]["internal_timeline"]["clips"]
    for index, clip in enumerate(clips, start=1):
        clip["id"] = f"picture-c{index:02d}"
        clip["app"] = {"cut": f"c{index:02d}", "layer": "picture"}

    code, out = run(capsys, doc, as_view="script", range="c06..c12")

    assert code == 0
    assert "picture-c06" in out and "picture-c12" in out
    assert "picture-c05" not in out and "picture-c13" not in out
    assert "could not convert string to float" not in out


def test_show_filter_names_matching_and_excluded_cuts(capsys):
    code, out = run(capsys, many_cuts(4), range="1..2")

    assert code == 0
    assert "filter: range 1..2 · 1 of 4 cuts match · 3 cuts excluded outside the selection" in out


def test_qualified_show_address_and_bounded_clip_spelling_share_edit_guidance(capsys):
    doc = bundle()
    doc["shots"]["A"]["internal_timeline"]["clips"][0]["app"] = {"cut": "c01", "layer": "picture"}
    doc["shots"]["A"]["internal_timeline"]["clips"][1]["app"] = {"cut": "c01", "layer": "rocket"}
    bundle_opener = opener(doc)

    def show(*argv):
        parser = cli.build_parser(None)
        parsed = parser.parse_args(["show", "--project", "p", "t", *argv])
        parsed.client = SimpleNamespace(timelines=SimpleNamespace(open_bundle=bundle_opener))
        assert parsed.handler(parsed) == 0
        return capsys.readouterr().out

    positional = show("c01.rocket.x")
    retained_flag = show("--clip", "c01.rocket.x")

    assert positional == retained_flag
    assert "c01.rocket.x · am-sprite param" in positional
    assert "next: timelines edit t --project p --clip c01.rocket --set x=VALUE" in positional


def test_show_sheet_script_frame_index_and_revision_diff_agree_without_rendering(monkeypatch):
    from astrid.core.timeline import scene_answers
    from astrid.sdk.timeline_cuts import diff_bundles
    from astrid.sdk.timeline_sheet import render_sheet

    doc = bundle()
    doc["shots"]["A"]["internal_timeline"]["clips"][0]["id"] = "picture-c01"
    doc["shots"]["A"]["internal_timeline"]["clips"][0]["app"] = {"cut": "c01", "layer": "picture"}
    doc["shots"]["B"]["internal_timeline"]["clips"][0]["id"] = "picture-c02"
    doc["shots"]["B"]["internal_timeline"]["clips"][0]["app"] = {"cut": "c02", "layer": "picture"}
    checkout = Checkout(copy.deepcopy(doc))
    table = build_cut_table(doc)
    sheet = render_sheet(checkout)
    script = render_script(doc)
    index_rows = checkout.cut_index.rows
    frame_index = [
        {
            "cut_id": row["cut_id"],
            "start_frame": round(row["start"] * FPS),
            "end_frame": round(row["end"] * FPS),
        }
        for row in index_rows
    ]

    changed = copy.deepcopy(doc)
    changed["shots"]["A"]["internal_timeline"]["clips"][0]["asset"] = "Q"
    revision_diff = diff_bundles(doc, changed)

    calls = []

    class Evaluator:
        def query_scene(self, *, revision_digest, scene, frames):
            calls.append((scene, tuple(frames)))
            return [
                {"frame": frame, "coverage": "COVERED", "content": "CONTENT", "findings": [], "layers": []}
                for frame in frames
            ]

    monkeypatch.setattr(scene_answers, "_client", lambda: Evaluator())
    first_shot = doc["shots"]["A"]["internal_timeline"]
    first_middle = (frame_index[0]["start_frame"] + frame_index[0]["end_frame"] - 1) // 2
    evaluated = scene_answers.frames_at(
        first_shot,
        [first_middle],
        FPS,
        {"width": 1920, "height": 1080},
    )

    assert [row["cut_id"] for row in table["rows"]] == ["c01", "c02"]
    assert [row["cut_id"] for row in frame_index] == ["c01", "c02"]
    assert [(row["start"], row["end"]) for row in table["rows"]] == [(0.0, 4.0), (4.0, 8.0)]
    assert all(cut_id in sheet for cut_id in ("c01", "c02"))
    assert "CUT 1  picture-c01" in script and "CUT 2  picture-c02" in script
    assert [(row["start_frame"], row["end_frame"]) for row in frame_index] == [(0, 120), (120, 240)]
    assert revision_diff["changes"][0]["clip_id"] == "picture-c01"
    assert revision_diff["changes"][0]["before"] == revision_diff["changes"][0]["after"] == [0.0, 4.0]
    assert [answer["frame"] for answer in evaluated] == [first_middle]
    assert calls and calls[0][1] == (first_middle,)


def test_cut_table_json_lists_every_layer_with_clip_id_and_every_word(capsys):
    code, out = run(capsys, bundle(), json=True)
    data = json.loads(out)["data"]
    rocket = next(r for r in data["rows"] if r["clip_id"] == "a-plate")
    assert [layer["clip_id"] for layer in rocket["layers"]] == ["a-rocket"]
    assert len(data["words"]) == 5
    assert {w["id"] for w in data["words"]} >= {"s1:2", "s2:1"}


def test_decimal_seconds_keeps_exact_rational_under_exact_key():
    converted = decimal_seconds({"start": [33276086721, 472037000], "text": "x"})
    assert converted["start"] == pytest.approx(70.494658, abs=1e-6)
    assert converted["start_exact"] == [33276086721, 472037000]
    assert converted["text"] == "x"


def test_paging_prints_showing_hint_and_next_page(capsys):
    code, out = run(capsys, many_cuts(60), page=2, page_size=20)
    assert code == 0
    assert "showing cuts 21–40 of 60" in out
    assert "next: --page 3" in out and "or --all" in out and "or --shot 1" in out and "or --range" in out
    assert "c20-plate" in out and "c39-plate" in out and "c40-plate" not in out


def test_page_rows_rejects_out_of_range_page():
    with pytest.raises(ValueError):
        page_rows([1, 2, 3], page=3, page_size=2)


def test_all_cuts_by_default_without_hint(capsys):
    _code, out = run(capsys, many_cuts(60))
    assert "showing cuts" not in out
    assert "c59-plate" in out


def test_script_has_word_onsets_and_layer_clip_ids():
    doc = bundle()
    text = render_script(doc)
    assert "s1:0–2" in text
    assert "s1:0–2  it 0.60 · went 0.90 · viral 1.30" in text
    assert "+ am-sprite R [a-rocket]" in text
    assert "s2:0–1  Live 6.00 · now 6.40" in text


def test_at_time_shows_cut_clips_words_and_next_cuts():
    doc = bundle()
    table = build_cut_table(doc)
    text = render_at(doc, table, 1.5, show_command="show")
    assert "cut 1 of 2" in text
    assert "a-rocket" in text and "a-plate" in text
    assert "▶ viral" in text and "[s1:2]" in text
    assert "next: --at 4.00" in text
    later = render_at(doc, table, 5.0)
    assert "next: --at 0.00" in later and "--at 4.00" not in later


def test_at_outside_timeline_is_none():
    doc = bundle()
    assert render_at(doc, build_cut_table(doc), 99.0) is None


def test_clips_lists_every_clip_in_a_shot_one_line_each():
    doc = bundle()
    text = render_clips(doc, {"A"})
    for clip_id in ("a-plate", "a-rocket", "a-vo", "a-music"):
        assert clip_id in text
    assert "b-plate" not in text
    assert "anchor 'viral' +0.10s" in text
    assert text.count("\n") == 4  # header + four clips


def test_working_copy_banner_and_edit_marks(tmp_path, monkeypatch, capsys):
    doc = bundle()
    edited = copy.deepcopy(doc)
    # A checkout carries its pinned base (as a published checkout does); then one clip moves.
    edited["base_placements"] = {row["occurrence_id"]: copy.deepcopy(row) for row in doc["placements"]}
    edited["base_parent_payload"] = copy.deepcopy(doc["parent"])
    for shot in edited["shots"].values():
        shot["base_internal_timeline"] = copy.deepcopy(shot["internal_timeline"])
    edited["shots"]["A"]["internal_timeline"]["clips"][1]["at"] = 1.5  # move a-rocket
    path = tmp_path / "draft.json"
    Checkout(edited).save(path, quiet=True)
    import astrid.sdk.timeline_checkout as checkout_module

    monkeypatch.setattr(checkout_module, "find_draft", lambda project, timeline, name="main", **kw: path)
    code, out = run(capsys, doc)
    assert code == 0
    first = out.splitlines()[0]
    assert first.startswith("WORKING COPY · 1 clip changed vs published rev-0 · --published")
    assert any(line.startswith("✎") and "0.00" in line for line in out.splitlines())

    code, out = run(capsys, doc, json=True)
    working = json.loads(out)["data"]["working_copy"]
    assert working["draft"] == str(path) and working["edits"] == 1 and working["base_revision"] == "rev-0"

    code, out = run(capsys, doc, published=True)
    assert not out.startswith("WORKING COPY")
    assert not any(line.lstrip().startswith("✎") for line in out.splitlines())
