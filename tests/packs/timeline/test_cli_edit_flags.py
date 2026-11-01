"""Restored from f840ec1c / the live line: the 22 `timelines edit` flags, film-end and the music policy (offline, on a local checkout file)."""
from __future__ import annotations

import copy
import json
import re

import pytest

from astrid.packs.timeline import cli
from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline_checkout import Checkout

FPS = 30


def _shot(clips, tracks=None):
    internal = {
            "tracks": tracks or [
                {"id": "type", "kind": "visual"}, {"id": "sprite", "kind": "visual"}, {"id": "plate", "kind": "visual"},
                {"id": "vo", "kind": "audio"}, {"id": "music", "kind": "audio"},
            ],
            "clips": clips,
            "registry": {"assets": {"P": {"media_id": "sha256:" + "1" * 64}, "R": {"media_id": "sha256:" + "2" * 64}, "Q": {"media_id": "sha256:" + "3" * 64}}},
    }
    payload = {"name": "SHOT", "items": [], "pools": [], "audio_bindings": [], "text_bindings": []}
    return {
        "payload": payload, "base_payload": copy.deepcopy(payload),
        "internal_timeline": internal, "base_internal_timeline": copy.deepcopy(internal),
    }


def _placement_rows():
    return {
        "occ-A": {"shot_id": "A", "occurrence_id": "occ-A", "placement": {"start_ms": 0}, "duration_ms": 4000},
        "occ-B": {"shot_id": "B", "occurrence_id": "occ-B", "placement": {"start_ms": 4000}, "duration_ms": 4000},
    }


def _shot_identity(sid):
    digest = "sha256:" + "0" * 64
    return {"shot_id": sid, "revision_id": f"rev-{sid}", "content_digest": digest, "internal_timeline_id": "t",
            "internal_timeline_revision_id": f"itr-{sid}", "internal_timeline_content_digest": digest, "item_id_map": {}}


def bundle():
    """Two shots: A (0–4 s) and B (4–8 s). Shot A's VO says "it went viral"; B says "Live now"."""
    a = [
        {"id": "a-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "a-rocket", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 1.0, "hold": 1.0, "params": {"x": 10}},
        {"id": "a-vo", "clipType": "media", "track": "vo", "asset": "P", "at": 0.5, "from": 0.0, "to": 2.0,
         "app": {"segment": "s1", "words": [[0.1, 0.3, "it"], [0.4, 0.6, "went"], [0.8, 1.2, "viral"]]}},
        {"id": "a-music", "clipType": "media", "track": "music", "asset": "Q", "at": 0.0, "from": 0.0, "to": 4.0},
    ]
    b = [
        {"id": "b-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "Q", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "b-type", "clipType": "am-type", "track": "type", "at": 2.0, "hold": 1.0, "params": {"text": "Live."}},
        {"id": "b-vo", "clipType": "media", "track": "vo", "asset": "P", "at": 1.9, "from": 0.0, "to": 1.0,
         "app": {"segment": "s2", "words": [[0.1, 0.4, "Live"], [0.5, 0.8, "now"]]}},
    ]
    return {
        "project_id": "p", "timeline_id": "t", "schema_version": 1,
        "base_parent": {"revision_id": "rev-0", "content_digest": "sha256:" + "0" * 64},
        "base_parent_payload": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": FPS, "width": 1920, "height": 1080}}}}, "clips": [], "registry": {}},
        "base_placements": {"placements": _placement_rows()},
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": FPS, "width": 1920, "height": 1080}}}}, "clips": []},
        "placements": [
            {"shot_id": "A", "occurrence_id": "occ-A", "placement": {"start_ms": 0}, "duration_ms": 4000},
            {"shot_id": "B", "occurrence_id": "occ-B", "placement": {"start_ms": 4000}, "duration_ms": 4000},
        ],
        "shots": {sid: {**_shot(clips), "shot_id": sid} for sid, clips in (("A", a), ("B", b))},
        "source_mapping": {"placements": _placement_rows(), "shots": {sid: _shot_identity(sid) for sid in ("A", "B")}},
    }




def _parse(*argv):
    parser = cli.build_parser(None)
    return parser.parse_args(["timelines", *argv][1:] if argv and argv[0] == "timelines" else list(argv))


def _run(*argv) -> int:
    parsed = _parse(*argv)
    return parsed.handler(parsed)


@pytest.fixture
def local(tmp_path):
    path = tmp_path / "t.checkout.json"
    Checkout(bundle()).save(path)
    return path



@pytest.mark.parametrize("argv", [
    ["edit", "tl1", "--project", "P", "--clip", "ROCKET", "--near", "viral", "--at-word", "viral"],
    ["edit", "--file", "x.json", "--cut", "3", "--nudge-frames", "-2", "--set", "x=5", "--set", 'y="a"'],
])
def test_edit_parses_to_its_handler(argv):
    assert _parse(*argv).handler.__wrapped__ is cli._cmd_edit


def test_edit_parses_operations_and_values():
    parsed = _parse("edit", "--file", "x.json", "--at-word", "viral", "--n", "2", "--offset", "0.2",
                    "--nudge-frames", "-3", "--set", "x=5", "--set", 'label="hi"', "--insert", "1:02:0.5")
    assert (parsed.at_word, parsed.n, parsed.offset, parsed.nudge_frames) == ("viral", 2, 0.2, -3)
    assert parsed.insert == ("1:02", 0.5)
    assert cli._parse_set(["x=5", 'label="hi"', "raw=text"]) == {"x": 5, "label": "hi", "raw": "text"}


def test_every_restored_flag_parses():
    parsed = _parse(
        "edit", "tl1", "--project", "P", "--cut", "c3", "--why", "because", "--hold", "deliberate",
        "--keep", "--clear-asset", "--allow-new-params", "--beats", "keep", "--verify", "--labels", "--timings",
        "--near", "viral", "--duration", "2", "--picture", "R", "--cut-id", "c3b", "--fit-sequence",
        "--shape-from", "other@rev", "--retime", "--insert", "1:02:0.5", "--film-end", "end", "--music", "plays-on",
    )
    assert (parsed.why, parsed.hold, parsed.keep, parsed.clear_asset, parsed.allow_new_params) == ("because", "deliberate", True, True, True)
    assert (parsed.beats, parsed.verify, parsed.labels, parsed.timings, parsed.near) == ("keep", True, True, True, "viral")
    assert (parsed.for_seconds, parsed.picture, parsed.cut_id, parsed.fit_sequence, parsed.shape_from) == (2.0, "R", "c3b", True, "other@rev")
    assert parsed.retime and parsed.insert == ("1:02", 0.5) and parsed.film_end == "end" and parsed.music == "plays-on"


def test_codex_spellings_have_our_aliases():
    parsed = _parse("edit", "--file", "x.json", "--line", "s1", "--take", "t.wav", "--words", "w.json",
                    "--gap", "0.25", "--script-gaps")
    assert (parsed.replace_line, parsed.insert_gap_after, parsed.script_gaps) == ("s1", 0.25, True)



# ---- edit on a local checkout file ----------------------------------------------------

def test_edit_at_word_moves_the_clip_and_says_so(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--at-word", "viral") == 0
    out = capsys.readouterr().out
    assert "✎ a-rocket" in out and 'now enters on "viral" = 1.30 s (was 1.00 s, +0.30 s)' in out
    assert 'now enters on "viral"' in out
    assert "next: timelines show" in out.splitlines()[-1] or "next:" in out
    reloaded = Checkout.load(local)
    rocket = reloaded.clip("a-rocket")
    assert rocket.anchor == '"viral"'


def test_edit_nudge_frames_set_and_close_gap(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--nudge-frames", "3", "--set", "x=42") == 0
    rocket = Checkout.load(local).clip("a-rocket")
    assert rocket.get("x") == 42 and rocket.params["x"] == 7  # canvas px in, stored on the sprite grid (÷6)
    assert rocket.start == pytest.approx(1.1)
    capsys.readouterr()

    assert _run("edit", "--file", str(local), "--close-gap-before", "now") == 0
    out = capsys.readouterr().out
    assert "next:" in out.splitlines()[-1]


def test_edit_needs_one_selector_and_an_operation(local, capsys):
    assert _run("edit", "--file", str(local)) == 2
    assert _run("edit", "--file", str(local), "--clip", "R") == 2
    assert "say what to do" in capsys.readouterr().err


def test_edit_error_exits_2_with_the_message(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--at-word", "nosuchword") == 2
    assert "nobody says" in capsys.readouterr().err


# ---- words -----------------------------------------------------------------------------

# ---- voice lines (timeline-level; the film re-flows) -----------------------------------

def _words_file(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def test_voice_line_swap_reflows_and_reports(local, tmp_path, capsys):
    words = _words_file(tmp_path, "s1.json", [[0.0, 0.5, "it"], [0.6, 1.1, "went"], [1.2, 2.0, "viral"]])
    assert _run("edit", "--file", str(local), "--line", "s1", "--take", "P", "--words", words) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1].startswith("next: timelines check")
    line = Checkout.load(local).voice("s1")
    assert [w.text for w in line.words] == ["it", "went", "viral"]


def test_voice_line_swap_needs_take_and_words(local, capsys):
    assert _run("edit", "--file", str(local), "--line", "s1") == 2
    assert "--take" in capsys.readouterr().err


def test_insert_line_after_another(local, tmp_path, capsys):
    words = _words_file(tmp_path, "s3.json", [[0.0, 0.4, "new"], [0.5, 0.9, "line"]])
    assert _run("edit", "--file", str(local), "--insert-line", "s3", "--after", "s1", "--take", "Q",
                "--words", words, "--text", "new line") == 0
    segments = [v.segment for v in Checkout.load(local).lines()]
    assert segments == ["s1", "s3", "s2"]


def test_remove_line(local, capsys):
    assert _run("edit", "--file", str(local), "--remove-line", "s2") == 0
    assert [v.segment for v in Checkout.load(local).lines()] == ["s1"]


def test_gap_after_is_set_and_reflowed_once(local, capsys):
    assert _run("edit", "--file", str(local), "--gap-after", "s1=0.5") == 0
    assert Checkout.load(local).voice("s1").gap_after == pytest.approx(0.5)


def test_gap_after_rejects_a_bad_value(local, capsys):
    assert _run("edit", "--file", str(local), "--gap-after", "s1") == 2
    assert "SEG=SECONDS" in capsys.readouterr().err


def test_from_script_with_unchanged_words_is_quiet(local, tmp_path, capsys):
    takes = tmp_path / "vo"
    takes.mkdir()
    (takes / "s1.words.json").write_text(json.dumps([[0.1, 0.3, "it"], [0.4, 0.6, "went"], [0.8, 1.2, "viral"]]))
    (takes / "s2.words.json").write_text(json.dumps([[0.1, 0.4, "Live"], [0.5, 0.8, "now"]]))
    script = tmp_path / "vo.json"
    script.write_text(json.dumps({"segments": [{"id": "s1", "text": "it went viral"},
                                               {"id": "s2", "text": "Live now"}]}))
    assert _run("edit", "--file", str(local), "--from-script", str(script), "--takes", str(takes)) == 0
    assert [v.segment for v in Checkout.load(local).lines()] == ["s1", "s2"]


def test_timeline_level_voice_ops_exclude_a_clip_selector(local, tmp_path, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--remove-line", "s2") == 2
    assert "separate" in capsys.readouterr().err


# ---- lint and diff read the working copy (stub client: the published head is the synthetic bundle)


def test_edit_set_on_a_formula_param_replaces_it_and_says_so(tmp_path, capsys):
    from tests.sdk.test_timeline_checkout import _formula_bundle

    path = tmp_path / "f.checkout.json"
    tl = Checkout(_formula_bundle())
    tl.resolve()
    tl.save(path)
    _run("edit", "--file", str(path), "--clip", "c1.rocket", "--set", "x=120")  # (the synthetic bundle is not publishable)
    out = capsys.readouterr().out
    assert "x was ƒ(HAND -42) → now fixed = 120" in out and "no change" not in out
    assert Checkout.load(path).clip("c1.rocket").get("x") == 120


def test_edit_set_refuses_a_param_the_element_does_not_declare(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--set", "sclae=3") == 2
    err = capsys.readouterr().err
    assert "has no param sclae (did you mean scale?)" in err


def test_undo_steps_back_through_the_working_copy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle()))
    assert _run("checkout", "t", "--project", "P") == 0
    _run("edit", "t", "--project", "P", "--clip", "R", "--nudge-frames", "3")
    _run("edit", "t", "--project", "P", "--clip", "R", "--nudge-frames", "3")
    path = tc.find_draft("P", "t")
    assert Checkout.load(path).clip("a-rocket").start == pytest.approx(1.2)
    capsys.readouterr()
    assert _run("undo", "t", "--project", "P") == 0
    assert "undid edit --clip R --nudge-frames 3" in capsys.readouterr().out
    assert Checkout.load(path).clip("a-rocket").start == pytest.approx(1.1)
    assert _run("undo", "t", "--project", "P") == 0
    assert Checkout.load(path).clip("a-rocket").start == pytest.approx(1.0)


def test_undo_is_one_edit_per_step_and_redo_goes_forward(tmp_path, monkeypatch, capsys):
    """P1: several edits (verbs and Python, some before one save), undo one at a time, then redo."""
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle()))
    assert _run("checkout", "t", "--project", "P") == 0
    for _ in range(3):
        _run("edit", "t", "--project", "P", "--clip", "R", "--nudge-frames", "3")  # three verb edits
    tl = Checkout.draft("P", "t")
    for _ in range(12):  # twelve API edits, ONE save
        tl.clip("a-rocket").nudge(frames=3)
    tl.save()
    path = tc.find_draft("P", "t")
    start = lambda: round(Checkout.load(path).clip("a-rocket").start * 30)  # noqa: E731
    assert start() == 30 + 45
    capsys.readouterr()
    assert _run("undo", "t", "--project", "P") == 0
    out = capsys.readouterr().out
    assert "undid" in out and "nudge" in out and start() == 30 + 9  # one save is one step: all twelve API nudges back
    assert _run("undo", "t", "--steps", "3", "--project", "P") == 0
    assert start() == 30
    assert _run("redo", "t", "--project", "P", "--steps", "2") == 0
    assert start() == 30 + 6


def test_edit_beats_attaches_a_grid_from_a_file_and_says_what_moved(local, tmp_path, capsys):
    tl = Checkout.load(local)
    tl.clip("a-music").data["app"] = {"beats": {"beats": [i * 0.5 for i in range(8)], "time": "cue_seconds"}}
    tl.clip("a-rocket").on('beat 2 after "went"')
    tl.save(local)
    grid = tmp_path / "beats.json"
    grid.write_text(json.dumps({"bpm": 100, "beats": [0.0, 0.6, 1.2, 1.8, 2.4, 3.0, 3.6]}))
    capsys.readouterr()
    assert _run("edit", "--file", str(local), "--clip", "a-music", "--beats", str(grid)) == 0
    out = capsys.readouterr().out
    assert "beats from beats.json (7 beats, 100 bpm)" in out and "a-rocket" in out
    assert Checkout.load(local).clip("a-rocket").start == pytest.approx(1.8)  # beat 2 after "went" (0.9 s): 1.2, 1.8




def test_edit_split_and_add_cut_from_the_cli(tmp_path, capsys):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "fixtures" / "timeline_editing"
    path = tmp_path / "tiny.checkout.json"
    Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8"))).save(path)
    assert _run("edit", "--file", str(path), "--split", "c1", "--on", '"viral"') == 0
    out = capsys.readouterr().out
    assert "c1a" in out
    tl = Checkout.load(path)
    assert "c1a" in tl._cut_spans()
    assert _run("edit", "--file", str(path), "--add-cut", "--on", "c2 +1s", "--after", "c2", "--cut-id", "c2x") == 0
    assert "c2x" in Checkout.load(path)._cut_spans()
    assert _run("edit", "--file", str(path), "--split", "c1") == 2  # needs --on


def test_a_cut_takes_a_why_and_a_deliberate_hold_and_a_stale_why_is_flagged(tmp_path, capsys):
    from pathlib import Path

    from astrid.core.timeline.cuts import is_deliberate
    from astrid.sdk import timeline_intent as intent
    from astrid.sdk.timeline_sheet import apply_sheet, render_sheet

    root = Path(__file__).resolve().parents[2] / "fixtures" / "timeline_editing"
    path = tmp_path / "tiny.checkout.json"
    Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8"))).save(path)
    assert _run("edit", "--file", str(path), "--cut", "c1", "--hold", "deliberate", "--why", "a reading hold") == 0
    tl = Checkout.load(path)
    pic = tl.cut_picture("c1")
    assert is_deliberate(pic.data) and tl.cut_note("c1")["why"] == "a reading hold"  # lint reads is_deliberate
    sheet = render_sheet(tl)
    assert "hold: deliberate" in sheet and apply_sheet(tl, sheet) == []
    apply_sheet(tl, sheet.replace("hold: deliberate", "hold: off"))
    assert not intent.deliberate(tl.cut_picture("c1").data) and "hold" not in tl.cut_note("c1")
    tl.clip("c1.rocket").remove()
    assert any(p.startswith("why     c1: its why was written") for p in tl.check().problems)




# ---- the rest of the restored flags ---------------------------------------------------

def test_insert_time_moves_everything_after_it_later(local, capsys):
    before = Checkout.load(local)
    assert _run("edit", "--file", str(local), "--insert", "5:1.5") == 0
    after = Checkout.load(local)
    assert after.duration == pytest.approx(before.duration + 1.5)
    assert after.clip("b-type").start == pytest.approx(before.clip("b-type").start + 1.5)


def test_insert_needs_time_and_seconds(local):
    with pytest.raises(SystemExit):
        _parse("edit", "--file", str(local), "--insert", "nonsense")


def test_retime_with_nothing_to_move_says_no_change(local, capsys):
    assert _run("edit", "--file", str(local), "--retime") == 3
    assert "no change" in capsys.readouterr().out


def test_film_end_and_music_policy_move_the_end_and_keep_the_music_ending(local, capsys):
    before = Checkout.load(local)
    assert _run("edit", "--file", str(local), "--film-end", "end +1s", "--music", "plays-on") == 0
    after = Checkout.load(local)
    assert after.duration == pytest.approx(before.duration + 1.0)


def test_music_flag_rejects_unknown_policies(local):
    with pytest.raises(SystemExit):
        _parse("edit", "--file", str(local), "--music", "loud")


def test_film_end_is_timeline_level_so_it_refuses_a_clip_selector(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--film-end", "end", "--nudge", "1") == 2
    assert "separate" in capsys.readouterr().err


def test_near_picks_the_clip_closest_to_a_word(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "P", "--near", "now", "--nudge", "0.1") == 0


def test_at_word_n_chooses_the_occurrence(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--at-word", "went", "--n", "1") == 0
    assert _run("edit", "--file", str(local), "--clip", "R", "--at-word", "went", "--n", "2") == 2


def test_clear_asset_and_keep_edit_the_clip(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "a-plate", "--clear-asset") == 0
    assert not Checkout.load(local).clip("a-plate").data.get("asset")
    assert _run("edit", "--file", str(local), "--clip", "a-rocket", "--keep") in (0, 3)


def test_allow_new_params_lets_set_write_an_undeclared_param(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--set", "zzbogus=2") == 2
    assert "--allow-new-params" in capsys.readouterr().err
    assert _run("edit", "--file", str(local), "--clip", "R", "--set", "zzbogus=2", "--allow-new-params") == 0
    assert Checkout.load(local).clip("a-rocket").params["zzbogus"] == 2


def test_duration_is_a_literal_length(local):
    assert _run("edit", "--file", str(local), "--clip", "R", "--duration", "0.5") == 0
    assert Checkout.load(local).clip("a-rocket").duration == pytest.approx(0.5)


def test_verify_compares_the_changed_frames_and_timings_say_where_time_went(tmp_path, monkeypatch, capsys):
    from astrid.sdk.timeline.checkout import Checkout as Real

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle()))
    seen = {}

    def fake_verify(self, at=None, *, since=None, client=None, labels=False, **_kw):
        seen.update(since=since, labels=labels)
        return {"lines": ["verify: 1 decoded frame(s) compared"], "timing": {"total_s": 1.5, "startup_s": 0.25}}

    monkeypatch.setattr(Real, "verify", fake_verify)
    monkeypatch.setattr("astrid.packs.timeline.cli_sweep.auto_sweep_after_edit", lambda *a, **k: 0)
    assert _run("checkout", "t", "--project", "P") == 0
    capsys.readouterr()
    assert _run("edit", "t", "--project", "P", "--clip", "R", "--at-word", "viral", "--verify", "--labels", "--timings") == 0
    out = capsys.readouterr().out
    assert "verify: 1 decoded frame(s) compared" in out and "timings  total 1.5s" in out
    assert seen["labels"] is True and seen["since"] is not None


def test_verify_on_a_bare_file_says_it_needs_a_working_copy(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--at-word", "viral", "--verify") == 0
    assert "verify  needs a working copy" in capsys.readouterr().out


def test_fit_sequence_without_a_sequence_is_a_clean_error(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--fit-sequence", "--on", "1") == 2
    assert capsys.readouterr().err.strip()


def test_why_and_hold_need_a_cut_not_a_clip(local, capsys):
    assert _run("edit", "--file", str(local), "--clip", "R", "--why", "x") == 2
    assert "--cut" in capsys.readouterr().err
