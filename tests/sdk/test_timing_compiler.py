"""Pure compiler cases converted from R4 onto main's authoring/transcript inputs."""
from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, replace
from fractions import Fraction
from pathlib import Path

import pytest

from astrid.core.timeline.authoring_bundle import _digest
from astrid.sdk.media_transcript import build_transcript, transcript_document_id
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.compiler import (
    TimingCompileError,
    TimingInputs,
    WordBinding,
    compile_timing,
)

MEDIA = "sha256:" + "1" * 64
VOICE = ("occ", "shot", "voice")
CARD = ("occ", "shot", "card")


def film(fps=30):
    """Main's bundle shape; words are v1 MEDIA source seconds, not clip-local rows."""
    return {
        "project_id": "project", "timeline_id": "timeline",
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": fps}}}},
                   "clips": []},
        "placements": [{"occurrence_id": "occ", "shot_id": "shot",
                        "placement": {"start_ms": 2000}, "duration_ms": 10000}],
        "shots": {"shot": {"internal_timeline": {
            "tracks": [{"id": "vo", "kind": "audio"}, {"id": "type", "kind": "visual"}],
            "registry": {"assets": {"take": {"media_id": MEDIA}}},
            "clips": [
                {"id": "voice", "clipType": "media", "track": "vo", "asset": "take",
                 "at": 1, "from": 2, "to": 6, "speed": 2},
                {"id": "card", "clipType": "am-type", "track": "type", "at": 0,
                 "hold": 1, "app": {"astrid": {"on": '"again" in line #2 +1f', "for": 0.5}}},
            ]}}},
    }


def captured(bundle=None):
    bundle = bundle or film()
    content = build_transcript(MEDIA, "line", "again again stop", [
        [2, 2.5, "again"], [3, 3.5, "again"], [5.5, 6, "stop"],
    ])
    document = {"document_id": transcript_document_id("project", MEDIA, content["source_id"]),
                "version": 7, "project_id": "project", "kind": "media.transcript", "content": content}
    document = {"document": document, "pin": {"document_id": document["document_id"],
                "version": 7, "content_digest": _digest(content)}}
    pin = WordBinding(CARD, "on", card(bundle)["app"]["astrid"]["on"], VOICE,
                      document["document"]["document_id"], 7, _digest(content), (content["segments"][0]["words"][1]["id"],))
    return bundle, document, pin


def card(bundle):
    return bundle["shots"]["shot"]["internal_timeline"]["clips"][1]


def project(bundle, document=None, pins=(), **kwargs):
    return compile_timing(TimingInputs(bundle, sources=[] if document is None else [document],
                                       bindings=pins, **kwargs))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def test_exact_binding_uses_selected_duplicate_word_and_preserves_authored_commands_and_clears():
    bundle, document, pin = captured()
    card(bundle).update(params={"clear": None, "opaque": [False, {"strange": "💫"}]}, to=None)
    bundle["commands"] = [{"op": "clip.move", "args": {"to": None}, "opaque": [1, None]}]
    bundle["parent"]["config"]["app"] = {"other": None}
    before = encoded([bundle, document])
    result = project(bundle, document, [pin])
    interval = result.timings[CARD]
    assert (interval["start"], interval["end"], interval["duration"]) == (
        Fraction(53, 15), Fraction(121, 30), Fraction(1, 2))
    assert (interval["start_frame"], interval["end_frame"]) == (106, 121)
    assert encoded(result.bundle) == encoded(bundle)
    assert encoded([bundle, document]) == before
    assert project(bundle, document, [pin]) == result
    assert project(result.bundle, document, [pin]) == result
    result.bundle["commands"][0]["args"]["to"] = "output only"
    assert encoded([bundle, document]) == before


def test_inputs_capture_latest_once_and_are_deeply_immutable():
    bundle, document, pin = captured()
    inputs = TimingInputs(bundle, sources=[document], bindings=[pin])
    expected = compile_timing(inputs)
    document["document"]["version"] = 8
    document["document"]["content"]["segments"][0]["words"][1]["start"] = 100
    card(bundle)["app"]["astrid"]["on"] = None
    inputs.bundle["shots"].clear()
    inputs.sources[0]["document"]["content"].clear()
    assert compile_timing(inputs) == expected
    with pytest.raises(FrozenInstanceError):
        inputs.policy = "floor"
    with pytest.raises(TypeError):
        expected.timings[CARD]["start"] = 0
    with pytest.raises(FrozenInstanceError):
        pin.version = 8


@pytest.mark.parametrize("fps", [{"numerator": 30000, "denominator": 1001}, [30000, 1001]])
def test_fixed_source_window_and_rational_half_frame_ties(fps):
    bundle = film(fps)
    card(bundle)["app"]["astrid"] = {"on": None, "until": "", "for": None}
    # JSON decimals cannot encode a rational tie exactly. 1001/2000 seconds
    # is exactly 15 frames at this rate; a source rate of 2 makes 7.5 frames.
    bundle["placements"][0]["placement"]["start_ms"] = 0
    card(bundle).update(at=0, hold=0.5005, speed=2)
    interval = project(bundle).timings[CARD]
    assert interval["end"] == Fraction(1001, 4000)
    assert interval["end_frame"] == 8  # exact tie rounds up, using main's units helper
    assert interval["applied_end"] == Fraction(1001, 3750)
    voice = project(bundle).timings[VOICE]
    assert (voice["start"], voice["end"], voice["duration"]) == (1, 3, 2)
    assert encoded(project(bundle).bundle) == encoded(bundle)


def test_after_word_end_at_source_out_is_included_and_onset_at_source_out_is_refused():
    bundle, document, pin = captured()
    text = 'after "stop"'
    card(bundle)["app"]["astrid"].update(on=text)
    word = document["document"]["content"]["segments"][0]["words"][-1]
    pin = replace(pin, declaration=text, word_ids=(word["id"],))
    assert project(bundle, document, [pin]).timings[CARD]["start"] == 5
    word.update(start=6, end=6.25)
    document["pin"]["content_digest"] = _digest(document["document"]["content"])
    pin = replace(pin, content_digest=document["pin"]["content_digest"])
    before = encoded([bundle, document])
    with pytest.raises(TimingCompileError, match="half-open"):
        project(bundle, document, [pin])
    assert encoded([bundle, document]) == before


def test_phrase_binding_offsets_and_until_are_not_replaced_by_cached_seconds():
    bundle, document, pin = captured()
    words = document["document"]["content"]["segments"][0]["words"]
    text = 'after "again again" +0.1s'
    card(bundle)["app"]["astrid"] = {"on": text, "until": 'after "stop" -1f', "for": 99}
    start = replace(pin, declaration=text, word_ids=tuple(w["id"] for w in words[:2]))
    end = replace(pin, field="until", declaration='after "stop" -1f', word_ids=(words[-1]["id"],))
    timing = project(bundle, document, [start, end]).timings[CARD]
    assert (timing["start"], timing["end"]) == (Fraction(23, 6), Fraction(149, 30))
    assert card(project(bundle, document, [start, end]).bundle)["app"]["astrid"]["for"] == 99


@pytest.mark.parametrize("failure,match", [
    ("missing", "missing or revoked"), ("latest_only", "missing or revoked"),
    ("wrong_media", "identity"), ("wrong_document", "identity"),
    ("wrong_schema", "identity"), ("missing_word", "stable word is missing"),
    ("duplicate_word", "ambiguous stable"), ("duplicate_pin", "ambiguous captured binding"),
    ("duplicate_document", "ambiguous captured transcript"), ("stale", "missing exact"),
    ("missing_source", "source occurrence/clip is missing"),
])
def test_binding_refusals_are_atomic_and_never_repair_by_same_text(failure, match):
    bundle, document, pin = captured()
    sources, pins = [document], [pin]
    if failure == "missing":
        sources = []  # also the capture adapter's representation of a revoked source
    elif failure == "latest_only":
        document["document"]["version"] += 1
        document["pin"]["version"] += 1
    elif failure == "wrong_media":
        document["document"]["content"]["media_id"] = "sha256:" + "2" * 64
    elif failure == "wrong_document":
        pins = [replace(pin, document_id="other")]
        document["document"]["document_id"] = "other"
        document["pin"]["document_id"] = "other"
    elif failure == "wrong_schema":
        document["document"]["content"]["schema"] = "spike/transcript"
    elif failure == "missing_word":
        document["document"]["content"]["segments"][0]["words"].pop(1)  # identical text still exists
    elif failure == "duplicate_word":
        words = document["document"]["content"]["segments"][0]["words"]
        words.append(copy.deepcopy(words[1]))
    elif failure == "duplicate_pin":
        pins.append(pin)
    elif failure == "duplicate_document":
        sources.append(copy.deepcopy(document))
    elif failure == "stale":
        card(bundle)["app"]["astrid"]["on"] = '"again" #1'
    elif failure == "missing_source":
        pins = [replace(pin, source=("another-occurrence", "shot", "voice"))]
    if failure in ("wrong_media", "wrong_schema", "missing_word", "duplicate_word"):
        # These are well-pinned new fixtures testing semantic refusal, not corrupt bytes.
        digest = _digest(document["document"]["content"])
        document["pin"]["content_digest"] = digest
        pins = [replace(pins[0], content_digest=digest)]
    before = encoded([bundle, sources])
    with pytest.raises(TimingCompileError, match=match):
        compile_timing(TimingInputs(bundle, sources=sources, bindings=pins))
    assert encoded([bundle, sources]) == before


@pytest.mark.parametrize("patch,match", [
    ({"hold": -1}, "invalid literal"), ({"from": 6, "to": 2}, "invalid source"),
    ({"speed": 0}, "positive"), ({"at": -3}, "before its occurrence"),
    ({"hold": 0.0001}, "collapsed"), ({"time_map": {"opaque": [None]}}, "time_map"),
    ({"timeMap": {}}, "timeMap"), ({"duration_ms": 100}, "millisecond"),
])
def test_invalid_or_unsupported_intervals_refuse_the_whole_compile(patch, match):
    bundle = film()
    card(bundle)["app"]["astrid"] = {}
    card(bundle).update(patch)
    before = encoded(bundle)
    with pytest.raises(TimingCompileError, match=match):
        project(bundle)
    assert encoded(bundle) == before


@pytest.mark.parametrize("declaration", [
    {"on": '"again"'}, {"until": "end of c99"},
    {"for": -1}, {"formulas": {"hold": {"word": "line:0"}}},
    {"sequence": {"id": "steps", "index": 0}}, {"orphan": "keep old cache"},
])
def test_missing_anchors_and_unmigrated_evaluators_refuse_honestly(declaration):
    bundle = film()
    card(bundle)["app"]["astrid"] = declaration
    before = encoded(bundle)
    with pytest.raises(TimingCompileError):
        project(bundle)
    assert encoded(bundle) == before


@pytest.mark.parametrize(
    ("declaration", "grid", "expected"),
    [
        ('beat 1 after "again" in line #1', {"beats": [0, .5, 1, 1.5, 2]}, Fraction(7, 2)),
        ('downbeat 1 after "again" in line #1', {"downbeats": [0, 1, 2]}, Fraction(4)),
    ],
)
def test_main_numeric_beat_carrier_resolves_legacy_moment_without_mutating_neighbor(
        declaration, grid, expected):
    bundle, document, _pin = captured()
    internal = bundle["shots"]["shot"]["internal_timeline"]
    internal["tracks"].append({"id": "music", "kind": "audio"})
    internal["clips"].append({
        "id": "music", "clipType": "media", "track": "music", "asset": "music",
        "at": 0, "from": 0, "to": 4, "app": {"astrid": {"beats": grid}},
    })
    card(bundle)["app"]["astrid"] = {"on": declaration, "for": .5}
    bundle["parent"]["clips"].append({
        "id": "word-neighbor", "clipType": "am-type", "track": "type", "at": 9,
        "hold": 1, "params": {"label": "again"},
    })
    before_neighbor = copy.deepcopy(bundle["parent"]["clips"][-1])

    result = project(bundle, document)

    assert result.timings[CARD]["start"] == expected
    assert result.bundle["parent"]["clips"][-1] == before_neighbor
    assert encoded(bundle) == encoded(project(bundle, document).bundle)


def test_numeric_beat_carrier_refuses_unsupported_bars_alias():
    bundle, document, _pin = captured()
    internal = bundle["shots"]["shot"]["internal_timeline"]
    internal["tracks"].append({"id": "music", "kind": "audio"})
    internal["clips"].append({
        "id": "music", "clipType": "media", "track": "music", "asset": "music",
        "at": 0, "from": 0, "to": 4, "app": {"astrid": {"beats": {"bars": [0, 1, 2]}}},
    })
    card(bundle)["app"]["astrid"] = {"on": 'downbeat 1 after "again" in line #1', "for": .5}
    with pytest.raises(TimingCompileError, match="no downbeat"):
        project(bundle, document)


def test_main_numeric_beat_carrier_uses_child_speed_and_half_open_window():
    bundle, document, _pin = captured()
    internal = bundle["shots"]["shot"]["internal_timeline"]
    internal["tracks"].append({"id": "music", "kind": "audio"})
    internal["clips"].append({
        "id": "music", "clipType": "media", "track": "music", "asset": "music",
        "at": 0, "from": 0, "to": 6, "speed": 2,
        "app": {"astrid": {"beats": {"beats": [0, 2, 3, 6]}}},
    })
    card(bundle)["app"]["astrid"] = {"on": 'beat 1 after "again" in line #1', "for": .5}
    assert project(bundle, document).timings[CARD]["start"] == Fraction(7, 2)

    internal["clips"][-1]["app"]["astrid"]["beats"]["beats"] = [0, 2, 3, "bad"]
    with pytest.raises(TimingCompileError, match="malformed app.astrid.beats.beats"):
        project(bundle, document)

    for invalid in (True, None):
        internal["clips"][-1]["app"]["astrid"]["beats"]["beats"] = [0, 2, invalid]
        with pytest.raises(TimingCompileError, match="malformed app.astrid.beats.beats"):
            project(bundle, document)


def test_main_numeric_beat_carrier_missing_table_fails_closed():
    bundle, document, _pin = captured()
    internal = bundle["shots"]["shot"]["internal_timeline"]
    internal["tracks"].append({"id": "music", "kind": "audio"})
    internal["clips"].append({
        "id": "music", "clipType": "media", "track": "music", "asset": "music",
        "at": 0, "from": 0, "to": 4, "app": {"astrid": {"beats": {}}},
    })
    card(bundle)["app"]["astrid"] = {"on": 'beat 1 after "again" in line #1', "for": .5}
    with pytest.raises(TimingCompileError, match="no beat"):
        project(bundle, document)


def test_native_unrelated_move_recompiles_legacy_beat_and_guards_carrier_table():
    from astrid.sdk.timeline import commands
    from astrid.sdk.timeline.capture import capture_move

    fixture = Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json"
    bundle = json.loads(fixture.read_text())
    bundle["parent"]["clips"].append({
        "id": "beat-target", "clipType": "am-type", "at": 0, "hold": 1,
        "app": {"astrid": {"on": "beat 1 after c1", "for": .5}},
    })
    music = bundle["shots"]["A"]["internal_timeline"]["clips"][-1]
    music["app"] = {"astrid": {"beats": {"beats": [0, .5, 1, 1.5, 2]}}}

    move = capture_move(bundle, "occurrence:occ-A/clip:a-rocket", 1.0)
    plan = commands.execute(bundle, [move])
    target = next(row for row in plan.bundle["parent"]["clips"] if row["id"] == "beat-target")
    assert target["at"] == .5
    assert any(read["path"] == ["app", "astrid", "beats"]
               and read["ref"].endswith("/clip:a-music")
               for read in move["reads"])

    changed = copy.deepcopy(bundle)
    changed["shots"]["A"]["internal_timeline"]["clips"][-1]["app"]["astrid"]["beats"]["beats"] = [
        0, .25, 1
    ]
    with pytest.raises(commands.CommandError, match="original authored guard changed"):
        commands.execute(changed, [move])

    for field, value in (("hold", 3), ("duration", 3)):
        changed = copy.deepcopy(bundle)
        changed["shots"]["A"]["internal_timeline"]["clips"][-1][field] = value
        with pytest.raises(commands.CommandError, match="original authored guard changed"):
            commands.execute(changed, [move])

    changed = copy.deepcopy(bundle)
    changed["placements"][0]["placement"]["start_ms"] += 100
    with pytest.raises(commands.CommandError, match="original authored guard changed"):
        commands.execute(changed, [move])

    changed = copy.deepcopy(bundle)
    changed["shots"]["A"]["internal_timeline"]["tracks"][-1]["kind"] = "visual"
    with pytest.raises(commands.CommandError, match="original authored guard changed"):
        commands.execute(changed, [move])


@pytest.mark.parametrize(
    ("declaration", "carrier", "expected"),
    [
        ("beat 1 after c1", {"beats": [0, .5, 1, 1.5, 2]}, Fraction(1, 2)),
        ("downbeat 1 after c1", {"downbeats": [0, 1, 2]}, Fraction(1)),
    ],
)
def test_main_context_beats_matches_native_compiler_on_real_fixture(
        declaration, carrier, expected):
    from astrid.sdk.timeline.checkout import Checkout

    bundle = json.loads((Path(__file__).parents[1] /
                         "fixtures/timeline_editing/tiny_publishable.json").read_text())
    target = next(row for row in bundle["shots"]["A"]["internal_timeline"]["clips"]
                  if row["id"] == "a-rocket")
    target["app"]["astrid"] = {"on": declaration, "for": .5}
    music = next(row for row in bundle["shots"]["A"]["internal_timeline"]["clips"]
                 if row["id"] == "a-music")
    music["app"] = {"astrid": {"beats": carrier}}

    main_time = Checkout(copy.deepcopy(bundle)).time(declaration)
    native_time = project(bundle).timings[("occ-A", "A", "a-rocket")]["start"]
    assert main_time == float(expected)
    assert native_time == expected


def test_main_context_and_native_compiler_both_refuse_bars_alias_on_real_fixture():
    from astrid.sdk.timeline.checkout import Checkout

    bundle = json.loads((Path(__file__).parents[1] /
                         "fixtures/timeline_editing/tiny_publishable.json").read_text())
    declaration = "downbeat 1 after c1"
    target = next(row for row in bundle["shots"]["A"]["internal_timeline"]["clips"]
                  if row["id"] == "a-rocket")
    target["app"]["astrid"] = {"on": declaration, "for": .5}
    music = next(row for row in bundle["shots"]["A"]["internal_timeline"]["clips"]
                 if row["id"] == "a-music")
    music["app"] = {"astrid": {"beats": {"bars": [0, 1, 2]}}}

    with pytest.raises(TimelineEditError):
        Checkout(copy.deepcopy(bundle)).time(declaration)
    with pytest.raises(TimingCompileError, match="no downbeat"):
        project(bundle)


def test_second_occurrence_and_parent_clips_keep_their_own_identity_and_metadata():
    bundle = film()
    card(bundle)["app"]["astrid"] = {"on": None, "until": None, "for": None}
    bundle["shots"]["second"] = copy.deepcopy(bundle["shots"]["shot"])
    bundle["placements"].append({"occurrence_id": "other", "shot_id": "second",
                                  "placement": {"start_ms": 20000}, "duration_ms": 10000})
    bundle["parent"]["clips"] = [{"id": "title", "at": 0, "hold": 2, "app": None}]
    result = project(bundle)
    assert result.timings[CARD]["start"] == 2
    assert result.timings["other", "second", "card"]["start"] == 20
    assert result.timings["parent", "", "title"]["end"] == 2
    assert encoded(result.bundle) == encoded(bundle)


def test_first_word_picture_uses_main_take_in_point_policy():
    bundle, document, pin = captured()
    text = '"again" in line #1'
    card(bundle).update(track="plate")
    card(bundle)["app"]["astrid"].update(cut="c1", on=text)
    pin = replace(pin, declaration=text,
                  word_ids=(document["document"]["content"]["segments"][0]["words"][0]["id"],))
    # Voice starts at 3s with source in=2 and speed=2. A cut on the first word
    # opens at the take's zero (2s), as main's Moment resolver specifies.
    assert project(bundle, document, [pin]).timings[CARD]["start"] == 2


def test_rational_fps_anchor_and_exact_frame_offset():
    bundle, document, pin = captured(film({"numerator": 30000, "denominator": 1001}))
    interval = project(bundle, document, [pin]).timings[CARD]
    assert interval["start_frame"] == 105
    assert interval["start"] == Fraction(7007, 2000)
    assert interval["end"] == Fraction(8007, 2000)


def test_explicit_clears_empty_formulas_and_opaque_maps_survive():
    bundle = film()
    card(bundle)["app"]["astrid"] = {"on": None, "until": "", "for": None,
                                        "formulas": {}, "sequence_fit": {}, "orphan": ""}
    card(bundle).update(hold=None, to=None, duration=1, speed=None, time_map=None,
                        app_opaque={"timeMap": {"plugin": [None, False]}})
    result = project(bundle)
    assert result.timings[CARD]["duration"] == 1
    assert encoded(result.bundle) == encoded(bundle)


def test_compilation_has_no_io_random_clock_renderer_or_mutable_checkout_dependency(monkeypatch):
    import builtins
    import importlib
    import socket
    import subprocess
    import time
    import uuid
    from pathlib import Path

    # Module imports precede the purity boundary (Python itself reads module files).
    importlib.import_module("astrid.sdk.timeline._timing_evaluators")
    from astrid.sdk.timeline_checkout import Checkout

    bundle, document, pin = captured()
    inputs = TimingInputs(bundle, sources=[document], bindings=[pin])

    def forbidden(*args, **kwargs):
        raise AssertionError("compiler used a side effect or mutable legacy evaluator")

    with monkeypatch.context() as patch:
        for obj, names in ((builtins, ["open"]), (Path, ["read_text", "read_bytes", "write_text"]),
                           (time, ["time", "monotonic"]), (uuid, ["uuid4"]),
                           (socket, ["socket", "create_connection"]), (subprocess, ["run", "Popen"]),
                           (Checkout, ["__init__", "check", "retime", "resolve", "reflow"])):
            for name in names:
                patch.setattr(obj, name, forbidden)
        assert compile_timing(inputs).timings[CARD]["start_frame"] == 106


@pytest.mark.parametrize("failure", ["corrupt_content", "binding_digest", "foreign_project", "header_version"])
def test_verified_document_content_header_and_complete_binding_pin_must_agree(failure):
    bundle, document, pin = captured()
    if failure == "corrupt_content":
        document["document"]["content"]["segments"][0]["words"][1]["start"] = 4
    elif failure == "binding_digest":
        pin = replace(pin, content_digest="sha256:" + "0" * 64)
    elif failure == "foreign_project":
        document["document"]["project_id"] = "other-project"
    else:
        document["document"]["version"] = 99
    before = encoded([bundle, document])
    with pytest.raises(TimingCompileError, match="digest|identity"):
        project(bundle, document, [pin])
    assert encoded([bundle, document]) == before
