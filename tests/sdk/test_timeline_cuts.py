"""The editor's cut table and revision diff (``astrid.sdk.timeline_cuts``) and their CLI routes."""

from __future__ import annotations

import copy
import json

import pytest

from astrid.core.cli.domain_product import run_product_family
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline_cuts import (
    base_bundle,
    build_cut_table,
    diff_bundles,
    filter_rows,
    render_cut_table,
    render_diff,
    render_summary,
    timecode,
)


def _bundle() -> dict:
    """Two shots: a 3-cut plate track with layers and timing-only VO words + narration."""
    tracks = [
        {"id": "type", "kind": "visual"},
        {"id": "plate", "kind": "visual"},
        {"id": "vo", "kind": "audio"},
    ]
    intro = {
        "payload": {"name": "01 INTRO", "text_bindings": [
            {"kind": "voiceover_script", "text": "One year ago. I invited testers."},
        ]},
        "internal_timeline": {"tracks": tracks, "clips": [
            {"id": "c01-plate", "track": "plate", "clipType": "am-snap-plate", "asset": "P-01", "at": 0.0, "hold": 2.0},
            {"id": "c01-type", "track": "type", "clipType": "am-type", "at": 0.0, "hold": 2.0, "params": {"text": "01."}},
            {"id": "c02-plate", "track": "plate", "clipType": "am-snap-plate", "asset": "P-02", "at": 2.0, "hold": 1.5},
            {"id": "c02-sprite", "track": "type", "clipType": "am-sprite", "asset": "C-02", "at": 2.5, "hold": 1.0},
            {"id": "c03-plate", "track": "plate", "clipType": "am-snap-plate", "asset": "P-01", "at": 3.5, "hold": 1.5},
            {"id": "vo-0", "track": "vo", "clipType": "media", "asset": "vo-s01", "at": 0.1, "from": 0.0, "to": 4.0,
             "app": {"words": [[0.0, 0.4], [0.4, 0.8], [0.8, 1.5], [2.1, 2.6], [2.6, 3.0], [3.5, 3.9]]}},
        ]},
    }
    outro = {
        "payload": {"name": "02 OUTRO"},
        "internal_timeline": {"tracks": tracks, "clips": [
            {"id": "c04-plate", "track": "plate", "clipType": "am-snap-plate", "asset": "P-03", "at": 0.0, "hold": 3.0},
            {"id": "vo-1", "track": "vo", "clipType": "media", "asset": "vo-s02", "at": 0.5, "from": 0.0, "to": 1.0,
             "app": {"words": [[0.0, 0.5, "Live."]]}},
        ]},
    }
    return {
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": 30}}}}},
        "placements": [
            {"shot_id": "intro", "occurrence_id": "occ-intro", "placement": {"start_ms": 0}, "duration_ms": 5000},
            {"shot_id": "outro", "occurrence_id": "occ-outro", "placement": {"start_ms": 5000}, "duration_ms": 3000},
        ],
        "shots": {"intro": intro, "outro": outro},
    }


def test_cut_table_rows_layers_and_words():
    table = build_cut_table(_bundle())
    assert table["fps"] == 30 and table["duration"] == 8.0
    rows = table["rows"]
    assert [row["clip_id"] for row in rows] == ["c01-plate", "c02-plate", "c03-plate", "c04-plate"]
    assert [(row["start"], row["end"]) for row in rows] == [(0.0, 2.0), (2.0, 3.5), (3.5, 5.0), (5.0, 8.0)]
    assert rows[1]["tc_in"] == "00:00:02:00" and rows[3]["tc_out"] == "00:00:08:00"
    assert rows[0]["layers"][0]["text"] == "01."
    assert rows[1]["layers"][0]["asset"] == "C-02" and rows[1]["layers"][0]["enters"] == 0.5
    # Timing-only words take the narration tokens when the counts agree.
    assert rows[0]["say"] == "One year ago."
    assert rows[1]["say"] == "I invited"
    assert rows[2]["say"] == "testers."
    assert rows[3]["say"] == "Live."
    assert table["word_sources"] == {
        "occ-intro": "app.words timing + narration text",
        "occ-outro": "app.words",
    }


def test_word_count_mismatch_is_reported_not_guessed():
    bundle = _bundle()
    bundle["shots"]["intro"]["payload"]["text_bindings"][0]["text"] = "Only three words"
    table = build_cut_table(bundle)
    assert table["rows"][0]["say"] is None and table["rows"][0]["words"] == 3
    assert "timing only (6 words) and the narration has 3" in table["word_sources"]["occ-intro"]
    assert "3 words (vo-0; no word text)" in render_cut_table(table)


def test_filters_by_range_and_shot_name_id_or_ordinal():
    table = build_cut_table(_bundle())
    assert [row["index"] for row in filter_rows(table, range_value="2.5..2.6")] == [2]
    assert [row["index"] for row in filter_rows(table, range_value="0:04..0:06")] == [3, 4]
    assert [row["index"] for row in filter_rows(table, shot="02 outro")] == [4]
    assert [row["index"] for row in filter_rows(table, shot="intro")] == [1, 2, 3]
    assert [row["index"] for row in filter_rows(table, shot="2")] == [4]
    with pytest.raises(ValueError, match="no shot matches 'nope'; shots: '01 INTRO'"):
        filter_rows(table, shot="nope")


def test_render_groups_rows_under_shots():
    table = build_cut_table(_bundle())
    text = render_cut_table(table, title="Timeline T")
    assert text.startswith("Timeline T\n0:08.00 (00:00:08:00) at 30 fps · 2 shots · 4 cuts")
    assert "01 INTRO · 0.00–5.00 s (5.00 s) · shot intro" in text
    assert "c02-plate  am-snap-plate P-02" in text
    assert '+ am-sprite C-02 @+0.50' in text
    assert 'VO "I invited"' in text
    assert timecode(165.9, 30) == "00:02:45:27"


def test_summary_of_a_film_with_several_shots_lists_the_shots_as_chapters_and_ignores_labels():
    bundle = _bundle()
    bundle["parent"]["config"]["chapters"] = [{"name": "STALE", "from": "c1"}, {"name": "STALE 2", "from": "c4"}]
    for clips in (bundle["shots"]["intro"]["internal_timeline"]["clips"],
                  bundle["shots"]["outro"]["internal_timeline"]["clips"]):
        for clip in clips:
            cut = {"c01-plate": "c1", "c02-plate": "c2", "c03-plate": "c3",
                   "c04-plate": "c4"}.get(clip["id"])
            if cut:
                clip["app"] = {"astrid": {"cut": cut}}

    summary = render_summary(build_cut_table(bundle), title="Timeline T")
    assert "2 chapters · 4 cuts" in summary
    assert "STALE" not in summary
    assert any("01 INTRO" in line and "0.00–5.00" in line and "3 cuts" in line for line in summary.splitlines())
    assert any("02 OUTRO" in line and "5.00–8.00" in line and "1 cuts" in line for line in summary.splitlines())


def test_picture_bed_falls_back_to_the_widest_visual_track():
    bundle = _bundle()
    for clip in bundle["shots"]["outro"]["internal_timeline"]["clips"]:
        if clip["track"] == "plate":
            clip["track"] = "picture"
    bundle["shots"]["outro"]["internal_timeline"]["clips"].append(
        {"id": "o-title", "track": "type", "clipType": "am-type", "at": 1.0, "hold": 0.5, "params": {"title": "END"}}
    )
    rows = [row for row in build_cut_table(bundle)["rows"] if row["shot_id"] == "outro"]
    assert [row["clip_id"] for row in rows] == ["c04-plate"] and rows[0]["track"] == "picture"
    assert rows[0]["layers"][0]["text"] == "END"


def test_diff_reports_a_roll_edit_in_timeline_seconds():
    before = _bundle()
    after = copy.deepcopy(before)
    clips = {clip["id"]: clip for clip in after["shots"]["intro"]["internal_timeline"]["clips"]}
    clips["c01-plate"]["hold"] = 2.5
    clips["c01-type"]["hold"] = 2.5
    clips["c02-plate"]["at"], clips["c02-plate"]["hold"] = 2.5, 1.0
    diff = diff_bundles(before, after)
    assert {change["clip_id"] for change in diff["changes"]} == {"c01-plate", "c01-type", "c02-plate"}
    assert diff["cut_points"]["moved"] == [{"from": 2.0, "to": 2.5, "delta": 0.5}]
    assert diff["windows"] == [[1.5, 3.0]]
    text = render_diff(diff)
    assert "cut moved 2.00 → 2.50 s (+0.50 s)" in text
    assert "c02-plate" in text and "2.00–3.50 → 2.50–3.50  dur 1.50 → 1.00" in text
    assert render_diff(diff_bundles(before, before)).startswith("No clip changes")


def test_base_bundle_reads_the_pinned_side_of_a_checkout():
    after = _bundle()
    candidate = copy.deepcopy(after)
    candidate["source_mapping"] = {"placements": {row["occurrence_id"]: copy.deepcopy(row) for row in after["placements"]}}
    for shot in candidate["shots"].values():
        shot["base_internal_timeline"] = copy.deepcopy(shot["internal_timeline"])
        shot["base_payload"] = copy.deepcopy(shot["payload"])
    candidate["shots"]["outro"]["internal_timeline"]["clips"][0]["asset"] = "P-09"
    diff = diff_bundles(base_bundle(candidate), candidate)
    assert [(c["clip_id"], c["fields"]) for c in diff["changes"]] == [("c04-plate", ["asset"])]


# ------------------------------------------------------------------- CLI

class _Timelines:
    def __init__(self, bundles: dict):
        self.bundles = bundles
        self.calls: list = []

    def open_bundle(self, project, ref, *, revision_id=None):
        self.calls.append((project, ref, revision_id))
        revision = revision_id or "head-2"
        bundle = copy.deepcopy(self.bundles[revision])
        # The immutable closure carries the identity of the exact parent
        # revision it contains; envelope head metadata is not readback proof.
        bundle.setdefault("base_parent", {})["revision_id"] = revision
        return DomainResult.success({
            "bundle": bundle, "project_id": "p-1", "timeline_id": "T1",
            "revision_id": revision, "head_revision_id": "head-2", "is_current_head": revision == "head-2",
        })

    def open_composition(self, project, ref, **kwargs):  # the --layers route
        return DomainResult.success({"kind": "timeline-inspection", "summary": {"revision_id": "head-2"},
                                     "scope": {"timeline": ref}, "query": {}, "clips": [], "pagination": {}})


class _Client:
    def __init__(self, bundles):
        self.timelines = _Timelines(bundles)


def test_show_prints_the_cut_table_by_default(capsys):
    client = _Client({"head-2": _bundle()})
    assert run_product_family("timelines", ["show", "--project", "demo", "T1", "--range", "2..3"], client=client) == 0
    out = capsys.readouterr().out
    assert "Timeline T1 · project demo · current head" in out
    assert "showing 1" in out and 'VO "I invited"' in out
    assert "see these cuts: python3 -m astrid timelines visualize T1 --project demo --revision-id head-2 --view contact --range 2.00..3.50" in out
    assert "per-track layer rows: --layers" in out
    assert client.timelines.calls == [("demo", "T1", None)]


def test_show_layers_keeps_the_per_track_inspection(capsys):
    client = _Client({"head-2": _bundle()})
    assert run_product_family("timelines", ["show", "--project", "demo", "T1", "--layers"], client=client) == 0
    out = capsys.readouterr().out
    assert "Visual layers" in out and client.timelines.calls == []


def test_show_unknown_shot_lists_the_shots(capsys):
    client = _Client({"head-2": _bundle()})
    assert run_product_family("timelines", ["show", "--project", "demo", "T1", "--shot", "nope"], client=client) == 2
    assert "shots: '01 INTRO' (intro), '02 OUTRO' (outro)" in capsys.readouterr().out


def test_diff_compares_revisions_and_offers_before_after_views(capsys):
    after = _bundle()
    for clip in after["shots"]["intro"]["internal_timeline"]["clips"]:
        if clip["id"] == "c03-plate":
            clip["at"], clip["hold"] = 4.0, 1.0
    client = _Client({"head-1": _bundle(), "head-2": after})
    assert run_product_family("timelines", ["diff", "--project", "demo", "T1", "--from", "head-1"], client=client) == 0
    out = capsys.readouterr().out
    assert "Timeline T1: head-1 → head-2" in out
    # Starting c03 later without extending c02 opens a gap: a new picture-less cut.
    assert "cuts 4 → 5" in out and "cut added at 4.00 s" in out
    assert "--revision-id head-1 --range 3..4.5 --every-frames 5" in out
    assert "--revision-id head-2 --range 3..4.5 --every-frames 5" in out
    assert run_product_family("timelines", ["diff", "--project", "demo", "T1", "--from", "head-1", "--json"], client=client) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["from_revision"] == "head-1" and data["to_revision"] == "head-2"
    assert data["changes"][0]["clip_id"] == "c03-plate"


def test_diff_without_revisions_says_what_to_pass(capsys):
    assert run_product_family("timelines", ["diff", "--project", "demo", "T1"], client=_Client({})) == 2
    assert "--from <revision-id>" in capsys.readouterr().out


# ------------------------------------------------------------ script / code views

def _keyed_bundle() -> dict:
    bundle = _bundle()
    bundle["shots"]["intro"]["internal_timeline"]["clips"].append(
        {"id": "c02-flap", "track": "type", "clipType": "am-flap", "at": 2.0, "hold": 1.5,
         "params": {"values": [{"text": "ERR 1", "at": 0}, {"text": "ERR 4", "at": 15}]}}
    )
    return bundle


def test_script_view_interleaves_words_cuts_layers_and_keyframes():
    from astrid.sdk.timeline_views import render_script

    text = render_script(_keyed_bundle())
    lines = [line.strip() for line in text.splitlines()]
    order = [
        "0.00 ┃ CUT 1  c01-plate · P-01  Δ-0.10s to 'One'",
        '0.10 │ "One year ago."  [0.10–1.60]',
        "2.00 ┃ CUT 2  c02-plate · P-02  Δ-0.20s to 'I'",
        '2.20 │ "I invited"  [2.20–3.10]',
        "2.50 + am-sprite C-02 [c02-sprite]",
        "2.50 ◆ am-flap values[1] → ERR 4 (f15)",
    ]
    positions = [lines.index(item) for item in order]
    assert positions == sorted(positions)
    assert "02 OUTRO · 5.00–8.00 s · words: app.words" in text


def test_code_view_calls_elements_with_props_keyframes_and_sources():
    from astrid.sdk.timeline_views import clip_keyframes, render_code

    text = render_code(_keyed_bundle(), window=(2.1, 3.4))
    assert 'with shot("01 INTRO", id="intro", at=0.00, dur=5.00):' in text
    assert 'with cut(2, at=2.00, dur=1.50):  # 00:00:02:00 "I invited"' in text
    assert 'plate  = am_snap_plate(asset="P-02")  # id c02-plate' in text
    assert 'type   = am_sprite(asset="C-02")  # enters +0.50 id c02-sprite' in text
    assert "# ◆ 2.50 values[1] → ERR 4 (f15)" in text
    assert "c01-plate" not in text and "02 OUTRO" not in text
    assert clip_keyframes({"params": {"messages": [{"appearAt": 30, "author": "pom"}]}}, 10.0, 30) == [
        (11.0, "messages[0] → pom (f30)")
    ]


def test_show_as_script_and_diff_as_code(capsys):
    before = _keyed_bundle()
    after = _keyed_bundle()
    for clip in after["shots"]["intro"]["internal_timeline"]["clips"]:
        if clip["id"] == "c02-flap":
            clip["params"]["values"][1]["at"] = 30
    client = _Client({"head-1": before, "head-2": after})
    assert run_product_family("timelines", ["show", "--project", "demo", "T1", "--as", "script", "--shot", "1"], client=client) == 0
    out = capsys.readouterr().out
    assert "Script view" in out and "02 OUTRO" not in out and "◆ am-flap values[1] → ERR 4 (f30)" in out
    assert run_product_family("timelines", ["diff", "--project", "demo", "T1", "--from", "head-1", "--as", "code"], client=client) == 0
    out = capsys.readouterr().out
    assert "-            # ◆ 2.50 values[1] → ERR 4 (f15)" in out
    assert "+            # ◆ 3.00 values[1] → ERR 4 (f30)" in out


def test_picture_sequence_reads_canonical_and_legacy_with_canonical_precedence():
    from astrid.core.timeline.cuts import sequence_id
    assert sequence_id({"app": {"sequence": "old"}}) == "old"
    assert sequence_id({"app": {"sequence": "old", "astrid": {"sequence": {"id": "new", "index": 0}}}}) == "new"
    assert sequence_id({"app": {"sequence": "old", "astrid": {"sequence": None}}}) is None
    assert sequence_id({"app": {"astrid": {"sequence": {"index": 0}}}}) is None
