"""The cut sheet round-trips: what show prints, apply reads back; text, verb and code make the same change."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_sheet import SheetError, apply_sheet, moment_range, parse_sheet, render_sheet

from tests.sdk.test_timeline_checkout import FPS, bundle


def named():
    data = bundle()
    a, b = data["shots"]["A"]["internal_timeline"]["clips"], data["shots"]["B"]["internal_timeline"]["clips"]
    a[0]["app"] = {"cut": "c1", "layer": "plate", "why": "the opener"}
    a[1]["app"] = {"cut": "c1", "layer": "rocket", "on": '"viral"'}
    a[1]["at"] = 1.3
    b[0]["app"] = {"cut": "c2", "layer": "plate", "why": "live"}
    b[1]["app"] = {"cut": "c2", "layer": "card", "for": 1.0}
    b[0]["at"], b[0]["hold"] = 0.0, 4.0
    a[2]["app"]["text"] = "It went viral."
    b[2]["app"]["text"] = "Live now."
    return data


def test_the_sheet_reads_like_a_script():
    tl = Checkout(named())
    tl.resolve()
    text = render_sheet(tl)
    assert '  s1  "It went viral."' in text
    assert "┃ c1" in text and "┃ c2" in text
    assert 'rocket  sprite' in text and 'on "viral"' in text
    assert "card" in text and "for 1s" in text
    assert "why: the opener" in text


def test_applying_the_printed_sheet_changes_nothing():
    tl = Checkout(named())
    tl.resolve()
    assert apply_sheet(tl, render_sheet(tl)) == []


def test_text_verb_and_code_make_the_same_change():
    base = Checkout(named())
    base.resolve()
    by_text, by_code = Checkout(copy.deepcopy(base.bundle)), Checkout(copy.deepcopy(base.bundle))
    sheet = render_sheet(by_text).replace("for 1s", 'until "now"')
    lines = apply_sheet(by_text, sheet)
    by_code.clip("c2.card").until("now")
    assert by_text.document() == by_code.document()
    assert any('c2.card' in line and 'now holds until "now" = ends' in line for line in lines)


def test_a_fragment_touches_only_its_cuts_and_a_new_line_adds_a_layer():
    tl = Checkout(named())
    tl.resolve()
    fragment = "\n".join(line for line in render_sheet(tl).splitlines() if "c1" not in line and "rocket" not in line
                         and "opener" not in line and "plate" not in line or "c2" in line)
    fragment += '\n         type  stamp  type  "LIVE"  size=40  on "now"\n'
    lines = apply_sheet(tl, fragment)
    assert tl.clip("c2.stamp").start == pytest.approx(tl.word("now").start, abs=1 / FPS)
    assert any(line.startswith("+ c2.stamp") for line in lines)
    assert tl.clip("c1.rocket")  # untouched: its cut was not in the fragment


def test_errors_name_the_line():
    tl = Checkout(named())
    with pytest.raises(SheetError, match="line 2"):
        apply_sheet(tl, "  0.00 ┃ c1\n  plate x snap-plate P on \"nonsense-word\"\n")
    assert parse_sheet("lines\n  s1  \"x\"  gap 0.5\n")["lines"]["s1"]["gap"] == 0.5


def test_ranges_take_words_times_and_cuts():
    tl = Checkout(named())
    tl.resolve()
    assert moment_range(tl, '"went".."Live"') == (pytest.approx(0.9), pytest.approx(6.0))
    assert moment_range(tl, "1..c2")[1] == pytest.approx(8.0)  # cut ranges are inclusive: through the end of c2
    assert moment_range(tl, "c2") == (pytest.approx(4.0), pytest.approx(8.0))  # one cut
    assert moment_range(tl, "c1..c1") == (pytest.approx(0.0), pytest.approx(4.0))


def test_the_tiny_fixture_prints_its_golden_sheet_and_round_trips():
    """A tiny representative timeline (two cuts, two VO lines, music) and its sheet: the handover fixture."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    sheet = render_sheet(tl, film="tiny")
    assert sheet == (root / "tiny.sheet").read_text(encoding="utf-8")
    assert apply_sheet(tl, sheet) == []


def test_an_error_echoes_the_line_and_says_what_to_write():
    tl = Checkout(named())
    tl.resolve()
    sheet = render_sheet(tl).replace('on "viral"', 'on "virl"')
    with pytest.raises(SheetError) as err:
        apply_sheet(tl, sheet)
    text = str(err.value)
    assert "«" in text and 'on "virl"' in text  # the offending line itself
    assert "did you mean viral" in text


def test_two_lines_with_one_layer_name_are_an_error_never_a_silent_merge():
    tl = Checkout(named())
    tl.resolve()
    sheet = render_sheet(tl)
    sheet += '         type  dot  type  "•"  size=10  on "now"\n         type  dot  type  "•"  size=12  on "Live"\n'
    with pytest.raises(SheetError, match="already has a layer named 'dot'"):
        apply_sheet(tl, sheet)
    assert not [c for c in tl.clips() if c.layer_name == "dot"]  # nothing was applied


def test_the_music_bed_is_in_the_sheet_and_its_line_edits_it():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    sheet = render_sheet(tl, film="tiny")
    assert "\nsound " in sheet and "music  a-music  media  Q  for 4s" in sheet
    changes = apply_sheet(tl, sheet.replace("media  Q  for 4s", "media  Q  volume=0.5  for 3s"))
    music = tl.clip("a-music")
    assert music.duration == pytest.approx(3.0) and music.data["volume"] == 0.5 and changes
    # a sheet without the sound section leaves the bed alone
    assert apply_sheet(tl, sheet.split("\nsound ")[0] + "\n") == []
    assert tl.clip("a-music").duration == pytest.approx(3.0)


def test_a_new_cut_header_with_a_plate_line_adds_the_cut():
    """A sheet can add a cut: ┃ c1b on "viral" + a plate line (its picture); the cut before ends there."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    changes = apply_sheet(tl, '  1.30 ┃ c1b on "viral"\n         plate  plate2  snap-plate  Q\n         why: the turn\n')
    assert any(line.startswith("+ c1b.plate2") for line in changes)
    spans = tl._cut_spans()
    assert spans["c1"][1] == pytest.approx(spans["c1b"][0]) == pytest.approx(tl.word("viral").start, abs=1 / 30)
    assert "┃ c1b   on \"viral\"" in render_sheet(tl)
    with pytest.raises(SheetError, match="needs a plate line"):
        apply_sheet(tl, '  1.30 ┃ c1c on "went"\n         type  t  type  "x"  for 1s\n')


def test_a_layer_added_in_python_takes_a_moment_and_its_name_and_shows_in_the_sheet():
    """T04d (1)(3)(4): tl.add(at=MOMENT, layer=NAME) joins the cut on screen, keeps its name, shows in the sheet."""
    import json
    from pathlib import Path

    from astrid.sdk.timeline_address import resolve

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    clip = tl.add("am-type", at='"Live" +2f', layer="tile-16-outline", params={"text": "LIVE"}, hold=0.5)
    assert clip.address == "c2.tile-16-outline" and intent.on(clip.data) == '"Live" +2f'
    assert clip.start == pytest.approx(tl.word("Live").start + 2 / 30, abs=1 / 30)
    assert resolve(tl, "c2.tile-16-outline").clip.data is clip.data
    assert "tile-16-outline" in render_sheet(tl) and "for 0.5s" in render_sheet(tl)
    assert apply_sheet(tl, render_sheet(tl)) == []  # the sheet round-trips it: never a second copy
    assert len([c for c in tl.clips() if intent.layer_of(c.data) == "tile-16-outline"]) == 1
    with pytest.raises(Exception, match="already has a layer named"):
        tl.add("am-type", at=6.5, layer="tile-16-outline", params={"text": "again"})


def test_split_a_cut_and_add_a_cut_in_python_then_the_sheet_round_trips():
    """T13: tl.cut("c1").split(on=…): the new cut takes the picture, later layers move, spanning ones carry."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    new = tl.cut("c1").split('"viral"')
    spans = tl._cut_spans()
    assert intent.cut_of(new.picture.data) == "c1a" and spans["c1"][1] == pytest.approx(spans["c1a"][0])
    assert spans["c1a"][0] == pytest.approx(tl.word("viral").start, abs=1 / 30)
    assert tl.clip("c1a.rocket").start == pytest.approx(spans["c1a"][0], abs=1 / 30)  # it starts on "viral": moved
    sheet = render_sheet(tl)
    assert "┃ c1a   on \"viral\"" in sheet and apply_sheet(tl, sheet) == []  # round-trips
    third = tl.add_cut("c2 +1s", after="c2", picture="P")
    assert intent.cut_of(third.picture.data) == "c2a" and third.picture.asset == "P"
    with pytest.raises(Exception, match="is inside c2a, not c1"):
        tl.add_cut("c2 +2s", after="c1")
    tl2 = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    with tl2.step("x"):
        pass
    tl2.cut("c1").split(1.0)  # a time: written against its cut
    assert intent.on(tl2.cut("c1a").picture.data) == "c1 +1s"


def test_a_clip_in_no_cut_shows_in_the_sheet_and_applying_never_duplicates_it():
    """T04d (2): a clip added without a cut used to be invisible; writing it into the sheet made a second copy."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    loose = tl.clip("c2.card")
    intent.set_cut(loose.data, None)
    intent.set_layer(loose.data, None)
    tl._mcache = None
    sheet = render_sheet(tl)
    assert loose.id in sheet  # shown, by its clip id, in the cut on screen
    before = len(tl.clips())
    assert apply_sheet(tl, sheet) == [] and not intent.cut_of(loose.data)  # unchanged: left as it was
    apply_sheet(tl, sheet.replace('"Live."', '"Live!"'))
    assert len(tl.clips()) == before and intent.cut_of(loose.data) == "c2"  # edited: it joins the cut, one clip
    assert loose.duration == pytest.approx(1.0) and loose.text == "Live!"  # with its own length
    with pytest.raises(SheetError):
        apply_sheet(tl, sheet.replace('on "viral"', 'on "virl"'))
    assert len(tl.clips()) == before  # a refused apply changes nothing


@pytest.mark.parametrize("explicit_length", [False, True])
@pytest.mark.parametrize("layer_name", [None, "loose-card"])
def test_edited_loose_layer_records_replayable_membership_and_preserves_its_payload(explicit_length, layer_name):
    import json
    from pathlib import Path

    from astrid.sdk.timeline import commands

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    loose = tl.clip("c2.card")
    intent.set_cut(loose.data, None)
    intent.set_layer(loose.data, layer_name)
    if not explicit_length:
        intent.set_for(loose.data, None)
    foreign = {"cut": "opaque", "nested": [None, False, {"value": 7}]}
    loose.data.setdefault("app", {})["foreign"] = copy.deepcopy(foreign)
    tl._mcache = None
    replay = Checkout(copy.deepcopy(tl.bundle))
    identities = {commands.scoped_clip(tl, clip) for clip in tl.clips()}
    start, duration = loose.start, loose.duration
    sheet = render_sheet(tl)
    assert apply_sheet(tl, sheet) == [] and intent.cut_of(loose.data) is None

    apply_sheet(tl, sheet.replace('"Live."', '"Live!"'))

    assert {commands.scoped_clip(tl, clip) for clip in tl.clips()} == identities
    assert tl.clip(loose.id).data is loose.data
    assert loose.text == "Live!" and loose.start == pytest.approx(start)
    assert loose.duration == pytest.approx(duration)
    assert intent.cut_of(loose.data) == "c2" and intent.layer_of(loose.data) == layer_name
    assert loose.data["app"]["foreign"] == foreign
    move = next(call for call in tl._pending_calls if call.op == "clip.move-to-cut")
    assert move.resolved == (commands.scoped_clip(tl, loose),)
    # With no remaining intent, the whole namespace is created and guarded.
    membership_path = "intent.cut" if explicit_length or layer_name else "app.astrid"
    assert move.expect[membership_path] == {"state": "absent"}
    assert move.authorship[membership_path] == "direct"
    assert all(call.created == () for call in tl._pending_calls)
    commands.run(replay, {"op": "timeline.batch", "select": None,
                          "args": {"commands": [call.to_json() for call in tl._pending_calls]}})
    assert replay.document() == tl.document()


def test_sheet_refuses_unrecorded_cut_membership_after_a_command_and_restores_the_document(monkeypatch):
    from astrid.sdk import timeline_sheet
    from astrid.sdk.timeline.commands import CommandError

    tl = Checkout(named())
    tl.resolve()
    sheet = render_sheet(tl)
    before = tl.document()
    update = timeline_sheet._update_layer

    def unrecorded_membership(timeline, clip, layer, **kwargs):
        update(timeline, clip, layer, **kwargs)
        if clip.id == tl.clip("c2.card").id:
            intent.set_cut(clip.data, None)

    monkeypatch.setattr(timeline_sheet, "_update_layer", unrecorded_membership)
    with pytest.raises(CommandError, match="Compiler prefix evidence does not match the executed candidate"):
        apply_sheet(tl, sheet.replace('"Live."', '"Live!"'))
    assert tl.document() == before


def test_a_words_of_formula_round_trips_and_can_be_set():
    """Acceptance fix 2: c09.we-shape words=ƒ(words of n05b) must apply unchanged (it read as a spoken word)."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    card = tl.clip("c2.card")
    card.data["params"]["words"] = []
    intent.set_formula(card.data, "params.words", {"words_of": "s2"})
    tl.resolve()
    sheet = render_sheet(tl)
    assert "words=ƒ(words of s2)" in sheet and apply_sheet(tl, sheet) == []
    card.set(words="ƒ(words of s1)")
    assert intent.formulas(card.data)["params.words"] == {"words_of": "s1"}
    with pytest.raises(Exception, match="no VO line 'zz'"):
        card.set(words="ƒ(words of zz)")


def test_a_range_list_picks_exactly_those_cuts():
    import json
    from pathlib import Path

    from astrid.sdk.timeline_sheet import range_cuts

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    tl.cut("c1").split('"viral"')
    assert range_cuts(tl, "c2,c1") == ["c1", "c2"] and range_cuts(tl, "c1..c1a") == ["c1", "c1a"]
    sheet = render_sheet(tl, cuts=range_cuts(tl, "c1,c2"))
    assert "┃ c1 " in sheet and "┃ c2 " in sheet and "┃ c1a" not in sheet
    assert apply_sheet(tl, sheet) == []


def test_detail_prints_long_values_as_editable_json_and_round_trips():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    card = tl.clip("c2.card")
    card.data["params"]["inset"] = {"top": 120, "left": 64, "right": 64, "bottom": 40}
    assert "inset=…" in render_sheet(tl)
    sheet = render_sheet(tl, detail=True)
    assert 'inset={"top":120,"left":64,"right":64,"bottom":40}' in sheet and apply_sheet(tl, sheet) == []
    apply_sheet(tl, sheet.replace('"top":120', '"top":90'))
    assert card.params["inset"]["top"] == 90


def test_a_cuts_notes_belong_to_the_cut_when_its_picture_changes():
    """Repro (c18): a why written while churn was the picture; churn moves to fx and another clip becomes the
    picture; a new why must be written, shown and diffed, and the hold must follow the picture role."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing"
    tl = Checkout(json.loads((root / "tiny.json").read_text(encoding="utf-8")))
    old_pic = tl.cut_picture("c2")
    old_pic.data["app"]["astrid"]["why"] = "the old note, on the old picture"  # how notes used to be kept
    old_pic.data["app"]["astrid"]["deliberate_hold"] = True
    tl.add("am-snap-plate", at=4.0, layer="lead", asset="P", track="plate")
    old_pic.set_track("type")  # the picture role moves to the new plate
    tl.retime()
    assert tl.cut_picture("c2").data is not old_pic.data
    assert tl.cut_note("c2") == {"why": "the old note, on the old picture", "hold": "deliberate"}
    assert intent.deliberate(tl.cut_picture("c2").data) and not intent.deliberate(old_pic.data)  # follows the role
    before = Checkout(json.loads(json.dumps(tl.bundle)))
    tl.set_cut_note("c2", why="s19: the new note")
    assert tl.cut_note("c2")["why"] == "s19: the new note" and "why: s19: the new note" in render_sheet(tl)
    from astrid.sdk.timeline_checkout import describe_changes
    assert any("c2" in line and "why note" in line for line in describe_changes(before, tl))
    assert intent.cut_notes(tl.bundle)["c2"]["why"] == "s19: the new note" and not intent.why(old_pic.data)
