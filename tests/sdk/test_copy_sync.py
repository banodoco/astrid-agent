"""Deterministic COPY-SYNC over disposable synthetic documents; no historical film."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.core.timeline import copy_sync as core
from astrid.core.timeline import moments
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import copy_sync as api
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commands import CommandError, run
from astrid.sdk.timeline.compiler import TimingInputs, compile_timing


def film(text="We waited 58 days.", display="58 days."):
    words = [[i * .3, (i + 1) * .3, word] for i, word in enumerate(text.split())]
    return Checkout({"project_id": "copy-fixture", "timeline_id": "synthetic-v11",
        "base_parent": {"revision_id": "r1", "content_digest": "sha256:synthetic"},
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": 30}}}}, "clips": [], "tracks": []},
        "placements": [{"shot_id": "A", "occurrence_id": "o1", "placement": {"start_ms": 0}, "duration_ms": 4000}],
        "shots": {"A": {"payload": {"text_bindings": [{"kind": "voiceover_script", "binding_id": "script-A", "content_hash": core.digest(text), "text": text}]},
            "internal_timeline": {"tracks": [{"id": "type", "kind": "visual"}, {"id": "vo", "kind": "audio"}],
                "clips": [{"id": "title", "clipType": "am-type", "track": "type", "at": .5, "hold": 1.5,
                           "params": {"text": display, "italicWord": "58"}, "app": {"foreign": {"keep": [1, True]}}},
                          {"id": "speech", "track": "vo", "at": 0, "hold": 2, "app": {"astrid": {"segment": "s1", "words": words}}}],
                "assets": {"assets": {}}}}}})


def row(tl, layer="occurrence:o1/clip:title", path="params.text"):
    return next(r for r in tl.copy_inventory()["rows"] if r["layer"] == layer and r["path"] == path)


def quote(tl, path="params.text", *, text_range=None, candidate=0):
    r = row(tl, path=path)
    display = {"path": path, "text": r["text"]}
    if text_range is None:
        source = r["matches"][candidate]["source"]
    else:
        display["range"] = text_range
        context = api.source_context(tl.bundle)
        line = context["lines"][0]
        words = core.runs(core.selected_display(display), line)[candidate]
        source = api._source_at(line, words)
    return {"version": 1, "normalization": core.NORMALIZATION, "display": display, "source": source}


def declare(tl, **kwargs):
    q = quote(tl, **kwargs)
    tl.clip("title").declare_quotes([q])
    return q


def change_words(tl, text):
    speech = tl.bundle["shots"]["A"]["internal_timeline"]["clips"][1]
    intent.set_line(speech, "s1", [[i * .3, (i + 1) * .3, word] for i, word in enumerate(text.split())])
    return tl


def codes(findings):
    return {r["code"] for f in findings for r in f["reasons"]}


@pytest.mark.parametrize("left,right,equal", [
    ("LIFETIME", "lifetime", True), ("58 days.", "58 days", True), ("READY:", "ready", True),
    ("‘Straße’\u2003cafe\u0301", "STRASSE café", True), ("DON’T", "don't", True),
    ("01", "1", False), ("58", "fifty-eight", False), ("01/02", "0102", False),
    ("-5 +5 $5 5%", "5 5 5 5", False), ("well-being", "wellbeing", False), ("café", "cafe", False),
])
def test_normalization_contract(left, right, equal):
    assert (core.normalized(left) == core.normalized(right)) is equal


def test_codepoint_positions_and_whole_source_words():
    text = '🪐 “Straße”\nCAFÉ!'
    ts = core.tokens(text)
    assert [text[slice(*t["range"])] for t in ts] == ["🪐", "Straße", "CAFÉ"]
    assert ts[1]["token"] == "strasse"
    line = {"segment": "s", "words": [{"id": "s:0", "text": "New York"}]}
    assert core.runs("New", line) == []
    assert len(core.runs("new york", line)) == 1


def test_default_moment_normalization_unchanged():
    from types import SimpleNamespace as W
    words = [W(text="01/02", segment="s")]
    assert moments.find_words(moments.Moment("word", text="0102"), words)
    assert not moments.find_words(moments.Moment("word", text="0102"), words, tokenizer=core.normalized)


def test_inventory_enumerates_all_fields_parent_scopes_headings_and_coverage_without_edits():
    tl = film()
    document = tl.bundle["shots"]["A"]["internal_timeline"]
    document["clips"] += [
        {"id": "callout", "clipType": "am-callout", "track": "type", "at": 0, "hold": 2,
         "params": {"title": "READY:\nV11", "body": "Agent goal: keep learning", "italicWord": "not copy"}},
        {"id": "card", "clipType": "text-card", "track": "type", "hold": 2, "params": {"content": "LIFETIME"}},
        {"id": "dynamic", "clipType": "am-type", "track": "type", "hold": 2, "params": {"text": {"expr": "secret"}}},
        {"id": "unknown", "clipType": "unsupported-visual", "track": "type", "hold": 2, "params": {"text": "not inferred"}},
    ]
    tl.bundle["placements"].append({"shot_id": "A", "occurrence_id": "o2", "placement": {"start_ms": 4000}, "duration_ms": 4000})
    tl.bundle["parent"]["tracks"] = [{"id": "type", "kind": "visual"}]
    tl.bundle["parent"]["clips"] = [{"id": "parent-title", "clipType": "am-type", "track": "type", "hold": 3, "params": {"text": "58 days."}}]
    before = json.dumps(tl.bundle, ensure_ascii=False)
    output = tl.copy_inventory()
    assert json.dumps(tl.bundle, ensure_ascii=False) == before
    assert row(tl)["status"] == "ambiguous"  # all occurrences, not nearest
    assert len(row(tl)["matches"]) == 2
    fields = {(r["layer"], r["path"]): r for r in output["rows"]}
    assert len(fields) == 13
    assert fields["occurrence:o1/clip:callout", "params.title"]["text"] == "READY:\nV11"
    assert fields["occurrence:o1/clip:callout", "params.body"]["status"] == "unmatched"
    assert fields["occurrence:o1/clip:card", "params.content"]["text"] == "LIFETIME"
    assert fields["occurrence:o2/clip:dynamic", "params.text"]["status"] == "unavailable"
    assert fields["occurrence:o1/clip:unknown", None]["limitations"]
    assert not any(r["path"] == "params.italicWord" for r in output["rows"])
    assert tl.copy_lint() == []  # unmatched headings are not errors
    assert json.dumps(tl.bundle, ensure_ascii=False) == before


def test_repeated_lines_are_all_candidates_never_cross_line_or_occurrence():
    tl = film("58 days 58 days", "58 days")
    assert row(tl)["status"] == "ambiguous"
    assert len(row(tl)["matches"]) == 2
    extra = copy.deepcopy(tl.bundle["shots"]["A"]["internal_timeline"]["clips"][1])
    extra["id"] = "speech2"
    intent.set_line(extra, "s2", [[0, .2, "58"], [.2, .4, "days"]])
    tl.bundle["shots"]["A"]["internal_timeline"]["clips"].append(extra)
    assert len(row(tl)["matches"]) == 3
    change_words(tl, "58")
    intent.set_line(extra, "s2", [[0, .2, "days"]])
    assert row(tl)["status"] == "unmatched"


def test_declaration_command_save_reload_undo_and_opaque_siblings(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    tl = film()
    path = tmp_path / "copy.json"
    tl.save(path, quiet=True)
    before = copy.deepcopy(tl.bundle)
    q = declare(tl)
    assert tl._pending_calls[-1].op == "clip.declare-quotes"
    assert any("app.astrid.copy_quotes" in key for key in tl._pending_calls[-1].authorship)
    tl.save(quiet=True)
    loaded = Checkout.load(path)
    assert intent.copy_quotes(loaded.clip("title").data) == [q]
    assert loaded.clip("title").data["app"]["foreign"] == {"keep": [1, True]}
    assert not loaded.copy_lint()
    loaded.undo()
    assert intent.copy_quotes(loaded.clip("title").data) == []
    assert loaded.clip("title").data["app"]["foreign"] == before["shots"]["A"]["internal_timeline"]["clips"][0]["app"]["foreign"]


def test_declaration_rejects_stale_display_source_duplicate_and_partial_words():
    tl = film(display="We waited 58 days.")
    q = quote(tl, text_range=[10, 17])
    with pytest.raises(CommandError, match="duplicate"):
        tl.clip("title").declare_quotes([q, q])
    q["display"]["range"] = [11, 17]
    with pytest.raises(CommandError, match="complete words"):
        tl.clip("title").declare_quotes([q])
    q = quote(tl)
    tl.clip("title").data["params"]["text"] = "different"
    with pytest.raises(CommandError, match="display expectation"):
        tl.clip("title").declare_quotes([q])
    tl = film()
    q = quote(tl)
    q["source"]["word_producer"]["words_digest"] = "wrong"
    with pytest.raises(CommandError, match="source expectation"):
        tl.clip("title").declare_quotes([q])


def test_partial_quote_persists_full_display_and_exact_codepoint_range():
    tl = film(display="🪐 58 days. — a heading")
    q = declare(tl, text_range=[2, 10])
    assert q["display"]["text"] == "🪐 58 days. — a heading"
    assert not tl.copy_lint()


def test_unique_insert_before_survives_without_refreshing_declaration():
    tl = film()
    q = declare(tl)
    before = copy.deepcopy(tl.bundle)
    change_words(tl, "Today We waited 58 days.")
    assert api.after_script(tl, before) == []
    assert intent.copy_quotes(tl.clip("title").data) == [q]
    state = intent.copy_quote_tracking(tl.clip("title").data)[core.digest(q)]
    assert [w["id"] for w in state["source"]["words"]] == ["s1:3", "s1:4"]
    assert not tl.copy_lint()


@pytest.mark.parametrize("fresh,reason", [
    ("We waited 59 days.", "replaced or deleted"),
    ("We waited", "replaced or deleted"),
    ("We waited 58 long days.", "contiguity"),
    ("We waited 58 days. 58 days.", "ambiguous"),
])
def test_stale_run_comparison(fresh, reason):
    tl = film()
    declare(tl)
    before = copy.deepcopy(tl.bundle)
    change_words(tl, fresh)
    findings = tl.copy_lint(before=Checkout(before))
    assert codes(findings) >= {"COPY_STALE", "COPY_SYNC"}
    assert any(reason in r["reason"] for f in findings for r in f["reasons"])


def test_deletion_then_reintroduction_never_resurrects_even_after_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    tl = film()
    q = declare(tl)
    before = copy.deepcopy(tl.bundle)
    change_words(tl, "We waited")
    api.after_script(tl, before)
    tl.save(tmp_path / "copy.json", quiet=True)
    tl = Checkout.load(tmp_path / "copy.json")
    before = copy.deepcopy(tl.bundle)
    change_words(tl, "We waited 58 days.")
    assert api.after_script(tl, before)
    assert "COPY_STALE" in codes(tl.copy_lint())
    assert intent.copy_quotes(tl.clip("title").data) == [q]
    tl.clip("title").declare_quotes([quote(tl)])  # explicit rebind repairs it
    assert not tl.copy_lint()


def test_source_raw_changes_and_display_case_do_not_make_lexical_mismatch():
    tl = film()
    declare(tl)
    before = copy.deepcopy(tl.bundle)
    change_words(tl, "WE WAITED 58 DAYS!")
    tl.clip("title").data["params"]["text"] = "58 DAYS!"
    fs = tl.copy_lint(before=Checkout(before))
    assert codes(fs) == {"COPY_RAW_CHANGE"}
    assert len(fs[0]["reasons"]) == 2
    api.after_script(tl, before)
    assert "COPY_STALE" not in codes(tl.copy_lint())


def test_timing_only_revision_and_snapshot_changes_are_not_lexically_stale():
    tl = film()
    declare(tl)
    tl.bundle["base_parent"]["revision_id"] = "r2"
    speech = tl.bundle["shots"]["A"]["internal_timeline"]["clips"][1]
    speech["hold"] = 2.5
    intent.set_line(speech, "s1", [[0, .5, "We"], [.5, 1, "waited"], [1, 1.5, "58"], [1.5, 2, "days."]])
    assert not tl.copy_lint()
    tl.clip("title").data["at"] = 2.5
    assert codes(tl.copy_lint()) == {"COPY_SYNC"}


def test_half_open_overlap_display_duration_and_shared_support():
    tl = film()
    declare(tl)
    tl.clip("title").data.update(at=1.2, hold=.5)
    assert codes(tl.copy_lint()) == {"COPY_SYNC"}  # speech ends exactly at display start
    tl.clip("title").data.update(at=-1, hold=1)
    assert codes(tl.copy_lint()) == {"COPY_SYNC"}  # display ends exactly at speech start
    tl.clip("title").data["hold"] = 1.01
    assert not tl.copy_lint()
    supplied = {"occurrence:o1/clip:title": [[3, 4]]}
    findings = tl.copy_lint(evaluated_support=supplied)
    assert findings[0]["support_evidence"] == "shared-evaluated-support"
    assert codes(findings) == {"COPY_SYNC"}


@pytest.mark.parametrize("change", ["replaced", "deleted", "lost-overlap"])
def test_three_substantive_inferred_lint_fixtures(change):
    before = film()
    after = Checkout(copy.deepcopy(before.bundle))
    if change == "replaced":
        change_words(after, "We waited 59 days.")
    elif change == "deleted":
        change_words(after, "We waited")
    else:
        after.clip("title").data.update(at=2, hold=1)
    fs = after.copy_lint(before=before)
    assert len(fs) == 1 and codes(fs) == {"COPY_SYNC_INFERRED"}
    assert fs[0]["reasons"][0]["evidence"]


def test_all_declared_layers_outside_change_intervals_and_multiple_fields_aggregate_once():
    tl = film()
    declare(tl)
    clips = tl.bundle["shots"]["A"]["internal_timeline"]["clips"]
    clips.append({"id": "callout", "clipType": "am-callout", "track": "type", "at": 20, "hold": 2,
                  "params": {"title": "58 days", "body": "58 DAYS!"}})
    declarations = []
    for path in ["params.title", "params.body"]:
        r = row(tl, "occurrence:o1/clip:callout", path)
        declarations.append({"version": 1, "normalization": core.NORMALIZATION,
                             "display": {"path": path, "text": r["text"]}, "source": r["matches"][0]["source"]})
    tl.clip("callout").declare_quotes(declarations)
    before = Checkout(copy.deepcopy(tl.bundle))
    change_words(tl, "We waited 59 days.")
    findings = tl.copy_lint(before=before)
    assert len(findings) == 2
    assert {f["layer"] for f in findings} == {"occurrence:o1/clip:title", "occurrence:o1/clip:callout"}
    assert {r["path"] for r in findings[1]["reasons"]} == {"params.title", "params.body"}


def test_missing_source_hidden_fields_and_no_read_mutation():
    tl = film()
    declare(tl)
    tl.bundle["shots"]["A"]["payload"]["text_bindings"] = []
    before = json.dumps(tl.bundle)
    assert row(tl)["status"] == "unavailable"
    assert codes(tl.copy_lint()) == {"COPY_SOURCE_UNAVAILABLE"}
    assert json.dumps(tl.bundle) == before
    tl = film()
    tl.bundle["shots"]["A"]["internal_timeline"]["tracks"][0]["hidden"] = True
    assert row(tl)["visibility"] == "hidden"
    assert row(tl)["status"] == "unavailable"
    assert not row(tl)["matches"][0]["nearby"]


def test_changed_display_and_source_line_identity_are_stale():
    tl = film()
    declare(tl)
    tl.clip("title").data["params"]["text"] = "59 days"
    assert codes(tl.copy_lint()) >= {"COPY_STALE", "COPY_SYNC"}
    tl = film()
    declare(tl)
    intent.set_line(tl.bundle["shots"]["A"]["internal_timeline"]["clips"][1], "new-line", [[0, 1, "58"], [1, 2, "days"]])
    assert codes(tl.copy_lint()) >= {"COPY_STALE", "COPY_SYNC"}


def test_real_apply_script_hook_and_detached_compiler_return_all_stale_layers(tmp_path, monkeypatch):
    from astrid.sdk.timeline import store
    monkeypatch.setattr(store, "import_media", lambda path, project: {"media_id": "synthetic-take"})
    tl = film()
    tl.bundle["shots"]["A"]["internal_timeline"]["clips"].append(
        {"id": "far-title", "clipType": "am-type", "track": "type", "at": 20, "hold": 2, "params": {"text": "58 DAYS"}})
    declare(tl)
    far = row(tl, "occurrence:o1/clip:far-title")
    tl.clip("far-title").declare_quotes([{"version": 1, "normalization": core.NORMALIZATION,
        "display": {"path": far["path"], "text": far["text"]}, "source": far["matches"][0]["source"]}])
    # A .wav existence marker is sufficient: inject no media reads or rendering.
    (tmp_path / "s1.wav").write_bytes(b"synthetic-existence-marker")
    (tmp_path / "s1.words.json").write_text(json.dumps({"words": [[0, .3, "We"], [.3, .6, "waited"], [.6, .9, "59"], [.9, 1.2, "days."]]}))
    spec = {"segments": [{"id": "s1", "text": "We waited 59 days."}]}
    frozen = copy.deepcopy(tl.bundle)
    result = compile_timing(TimingInputs(tl.bundle), [("apply_script", (spec,), {"takes": tmp_path})])
    assert tl.bundle == frozen
    assert sum("COPY_STALE" in line for line in result.values[0]) == 2
    assert any("COPY_STALE" in line for line in result._state["notes"])
    assert intent.copy_quote_tracking(result.bundle["shots"]["A"]["internal_timeline"]["clips"][0])
    report = tl.apply_script(spec, takes=tmp_path)
    assert sum("COPY_STALE" in line for line in report) == 2
    assert codes(tl.copy_lint()) >= {"COPY_STALE", "COPY_SYNC"}


def test_command_expectations_and_atomic_rollback():
    tl = film()
    q = quote(tl)
    command = {"op": "clip.declare-quotes", "select": "occurrence:o1/clip:title", "args": {"declarations": [q]},
               "expect": {"params.text": {"state": "present", "value": "wrong"}}}
    before = copy.deepcopy(tl.bundle)
    from astrid.sdk.timeline.commands import SemanticConflict
    with pytest.raises(SemanticConflict):
        run(tl, command)
    assert tl.bundle == before


def test_parent_quote_command_and_field_removal_stay_observable():
    tl = film()
    parent = {"id": "parent-text", "clipType": "am-callout", "track": "type", "at": 0, "hold": 1,
              "params": {"title": "58 days", "body": "58 days"}}
    tl.bundle["parent"]["tracks"] = [{"id": "type", "kind": "visual"}]
    tl.bundle["parent"]["clips"].append(parent)
    r = row(tl, "parent/clip:parent-text", "params.title")
    q = {"version": 1, "normalization": core.NORMALIZATION, "display": {"path": r["path"], "text": r["text"]}, "source": r["matches"][0]["source"]}
    tl.batch([{"op": "clip.declare-quotes", "select": "parent/clip:parent-text", "args": {"declarations": [q]}}])
    assert intent.copy_quotes(parent) == [q]
    parent["clipType"] = "am-type"
    assert "COPY_SOURCE_UNAVAILABLE" in codes(tl.copy_lint())
    assert len(tl.copy_lint()) == 1


def test_raw_malformed_declarations_fail_closed_and_do_not_disappear():
    tl = film()
    tl.clip("title").data.setdefault("app", {})["astrid"] = {"copy_quotes": "opaque malformed"}
    assert codes(tl.copy_lint()) == {"COPY_SOURCE_UNAVAILABLE"}
    before = copy.deepcopy(tl.bundle)
    assert api.after_script(tl, before)
    assert tl.clip("title").data["app"]["astrid"]["copy_quotes"] == "opaque malformed"


def test_lost_source_occurrence_and_removed_display_layer_report():
    tl = film()
    declare(tl)
    before = Checkout(copy.deepcopy(tl.bundle))
    tl.bundle["placements"][0]["occurrence_id"] = "new-occurrence"
    assert "COPY_STALE" in codes(tl.copy_lint())
    tl = Checkout(copy.deepcopy(before.bundle))
    tl.bundle["shots"]["A"]["internal_timeline"]["clips"].pop(0)
    fs = tl.copy_lint(before=before)
    assert len(fs) == 1 and codes(fs) == {"COPY_STALE"}


def test_tampered_context_is_rejected_and_source_only_track_is_labelled():
    tl = film()
    context = api.source_context(tl.bundle)
    tl.clip("title").data["params"]["text"] = "READY:"
    with pytest.raises(ValueError, match="does not belong"):
        api.inventory(tl, context=context)
    tl.bundle["shots"]["A"]["internal_timeline"]["clips"][0]["track"] = "source"
    assert row(tl)["visibility"] == "source-only"
    assert row(tl)["status"] == "unavailable"


def test_repeat_correspondence_can_be_forced_by_surrounding_context():
    tl = film("A 58 days B 58 days C", "58 days")
    declare(tl, candidate=1)
    before = copy.deepcopy(tl.bundle)
    change_words(tl, "Today A 58 days B 58 days C")
    assert api.after_script(tl, before) == []
    assert not tl.copy_lint()


def test_multi_step_shift_chain_and_timing_undo_preserve_original_provenance():
    tl = film()
    original = declare(tl)
    before = copy.deepcopy(tl.bundle)
    change_words(tl, "Today We waited 58 days.")
    api.after_script(tl, before)
    before = copy.deepcopy(tl.bundle)
    change_words(tl, "Now Today We waited 58 days.")
    api.after_script(tl, before)
    assert not tl.copy_lint()
    assert intent.copy_quotes(tl.clip("title").data) == [original]
    tracking = intent.copy_quote_tracking(tl.clip("title").data)[core.digest(original)]
    assert tracking["source"]["words"][0]["id"] == "s1:4"


def test_source_producer_change_without_correspondence_cannot_silently_rebind():
    tl = film()
    declare(tl)
    change_words(tl, "Today We waited 58 days.")
    # No before context/history has been supplied for this out-of-band replacement.
    assert "COPY_STALE" in codes(tl.copy_lint())


def test_schema_validates_actual_declaration_and_tracking():
    from jsonschema import Draft202012Validator
    tl = film()
    declare(tl)
    before = copy.deepcopy(tl.bundle)
    change_words(tl, "We waited 59 days.")
    api.after_script(tl, before)
    schema = json.loads((Path(__file__).parents[2] / "schemas/astrid-intent/astrid-intent.schema.json").read_text())
    validator = Draft202012Validator({"$ref": "#/$defs/clipIntent", "$defs": schema["$defs"]})
    validator.validate(tl.clip("title").data["app"]["astrid"])


@pytest.mark.parametrize("fresh,expected", [("WE WAITED 58 DAYS!", "COPY_RAW_CHANGE"), ("We waited 58 day-s.", "COPY_STALE")])
def test_apply_script_cannot_hide_raw_or_internal_punctuation_changes(tmp_path, monkeypatch, fresh, expected):
    from astrid.sdk.timeline import store
    monkeypatch.setattr(store, "import_media", lambda path, project: {"media_id": "synthetic-take"})
    tl = film()
    declare(tl)
    (tmp_path / "s1.wav").write_bytes(b"synthetic-existence-marker")
    (tmp_path / "s1.words.json").write_text(json.dumps({"words": [[i * .3, (i + 1) * .3, text] for i, text in enumerate(fresh.split())]}))
    report = tl.apply_script({"segments": [{"id": "s1", "text": fresh}]}, takes=tmp_path)
    assert sum(expected in r for r in report) == 1
    assert intent.words(tl.bundle["shots"]["A"]["internal_timeline"]["clips"][1])[-1][2] == fresh.split()[-1]
    if expected == "COPY_RAW_CHANGE":
        assert "COPY_STALE" not in codes(tl.copy_lint())


def test_apply_script_text_without_a_new_transcript_reports_unavailable(tmp_path):
    tl = film()
    declare(tl)
    report = tl.apply_script({"segments": [{"id": "s1", "text": "We waited 59 days."}]}, takes=tmp_path)
    assert sum("COPY_SOURCE_UNAVAILABLE" in line for line in report) == 1
    assert codes(tl.copy_lint()) == {"COPY_SOURCE_UNAVAILABLE"}
    assert row(tl)["status"] == "unavailable"


def test_known_line_deletion_is_stale_and_cannot_reintroduce_from_another_line():
    tl = film()
    declare(tl)
    before = copy.deepcopy(tl.bundle)
    tl.bundle["shots"]["A"]["internal_timeline"]["clips"].pop(1)
    assert any("COPY_STALE" in line for line in api.after_script(tl, before))
    assert "COPY_STALE" in codes(tl.copy_lint())


def test_default_lint_consumes_actual_stored_base_and_never_rebinds_from_it():
    tl = film()
    shot = tl.bundle["shots"]["A"]
    shot["base_internal_timeline"] = copy.deepcopy(shot["internal_timeline"])
    shot["base_payload"] = copy.deepcopy(shot["payload"])
    before = copy.deepcopy(tl.bundle)
    change_words(tl, "We waited 59 days.")
    assert codes(tl.copy_lint()) == {"COPY_SYNC_INFERRED"}
    assert any("COPY_SYNC_INFERRED" in line for line in tl.lint(cuts=[]))
    tl = Checkout(before)
    q = declare(tl)
    prior = copy.deepcopy(tl.bundle)
    change_words(tl, "Today We waited 58 days.")
    api.after_script(tl, prior)
    assert not tl.copy_lint()
    assert intent.copy_quotes(tl.clip("title").data) == [q]


def test_source_word_indices_match_f_read_order_for_multiple_producers():
    tl = film()
    clips = tl.bundle["shots"]["A"]["internal_timeline"]["clips"]
    earlier = copy.deepcopy(clips[1])
    earlier.update(id="earlier-producer", at=-1, hold=.5)
    intent.set_line(earlier, "s1", [[0, .2, "Early"], [.2, .4, "words"]])
    clips.append(earlier)  # deliberately stored after its later producer
    actual = {(w.clip_id, w.text, w.start): w.id for w in tl.words}
    adapted = {(line["clip"].split("/clip:")[1], w["text"], round(w["start"], 6)): w["id"]
               for line in api.source_context(tl.bundle)["lines"] for w in line["words"]}
    assert adapted == actual


def test_native_text_is_explicit_coverage_gap_until_its_definition_declares_slots():
    tl = film()
    tl.clip("title").data.update(clipType="text", text="native caption")
    native = tl.copy_inventory()["rows"][0]
    assert native["path"] is None and native["status"] == "unavailable"
    assert "native" in native["limitations"][0]
    assert native["text"] is None
