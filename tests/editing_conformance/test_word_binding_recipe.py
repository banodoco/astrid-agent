"""Executable design spike; product registry and default compiler stay untouched."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from fractions import Fraction

import pytest

from astrid.core.timeline import moments
from astrid.sdk.timeline import commands
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.compiler import TimingCompileError
from tests.editing_conformance import native_word_anchor_fixture as retained_inputs
from tests.editing_conformance.word_binding_recipe import OP, authored, compile_authored, install

pytestmark = pytest.mark.integration
retained = retained_inputs.current_retained
TARGET = retained_inputs.TARGET
REF = "parent/clip:card"
KEY = (REF, "on")


def mirrored_parent_clip_apps(bundle):
    clips = bundle["parent"]["clips"]
    mirrors = bundle["parent"]["config"]["clips"]
    assert [clip["id"] for clip in clips] == [clip["id"] for clip in mirrors]
    primary = next(clip for clip in clips if clip["id"] == "card")
    mirror = next(clip for clip in mirrors if clip["id"] == "card")
    assert primary == mirror
    bundle["parent"]["config"]["clips"] = clips
    current_mirrors = bundle["parent"]["config"]["clips"]
    primary = next(clip for clip in clips if clip["id"] == "card")
    mirror = next(clip for clip in current_mirrors if clip["id"] == "card")
    return [primary["app"]["astrid"], mirror["app"]["astrid"]]


@pytest.fixture
def recipe(retained, monkeypatch):
    install(monkeypatch)
    retained.submit(retained.create("recipe-initial"), "recipe-initial")
    return retained


def bundle(case):
    result = bundle_from_document(case.documents[case.head()["revision_id"]])
    # This is the same authorized capture observed from the real provider,
    # handed to the frontend picker, not an inline command/source authority.
    result["source_mapping"]["pinned_documents"] = copy.deepcopy(
        case.hydrated[-1]["source_mapping"]["pinned_documents"])
    return result


def pick(case, index=0, *, text='"again"', source="voice", count=1, field="on"):
    original = bundle(case)
    words = original["source_mapping"]["pinned_documents"][0]["document"]["content"]["segments"][0]["words"]
    call = {"schema_version": 1, "op": OP, "version": "1", "select": REF,
            "args": {"field": field, "declaration": text, "choice": {
                "source": "occurrence:occ/clip:" + source, "document": case.pin,
                "word_ids": [word["id"] for word in words[index:index + count]]}},
            "intent": {"anchor": "follow"}}
    moment = moments.parse(text)
    for name, default in (("edge", "start"), ("offset_s", 0), ("offset_frames", 0)):
        if getattr(moment, name) != default:
            call["args"]["choice"][name] = getattr(moment, name)
    return commands.execute(original, [call]).commands[0]


def history(case):
    # History is used only to inspect undo provenance, never to compile a film.
    page = case.service.list_timeline_commits(case.project, "film", limit=200, authorize=lambda: None)
    assert page["next_cursor"] is None
    assert page["head"] == case.head()
    return json.loads(json.dumps(page["items"]))


def projected(case):
    current = bundle(case)
    return compile_authored(current, current["source_mapping"]["pinned_documents"])


def undo(case, accepted, key):
    return case.submit(case.request([{"schema_version": 1, "op": "timeline.inverse", "version": "1",
        "select": None, "args": {"publications": [accepted["receipt"]["receipt_id"]]}}]), key)


def restore(case, accepted, key):
    parent = accepted["data"]["canonical_document"]["parent_revision"]
    return case.submit(case.request([{"schema_version": 1, "op": "timeline.restore", "version": "1",
        "select": None, "args": {name: parent[name] for name in ("revision_id", "content_digest")}}]), key)


def window_command(monkeypatch, start, end):
    """A bounded test-only source trim, using the ordinary recorder and Runtime."""
    registry = commands.operations

    def trim(ctx, command):
        target = command["select"]
        ctx.read(target, ["id"])
        for field in ("from", "to"):
            ctx.write(target, [field], command["args"][field])

    parameters = {"type": "object", "additionalProperties": False, "required": ["from", "to"],
                  "properties": {field: {"type": "number"} for field in ("from", "to")}}
    monkeypatch.setattr(commands, "operations", lambda: {
        **registry(), "fixture.window": commands.Operation(parameters, trim, {}, True)})
    validate = commands.validate_command

    def checked(command):
        if command.get("op") == "fixture.window":
            from jsonschema import Draft202012Validator
            commands._VALIDATOR.validate(command)
            Draft202012Validator(parameters).validate(command["args"])
        else:
            validate(command)

    monkeypatch.setattr(commands, "validate_command", checked)
    return {"schema_version": 1, "op": "fixture.window", "version": "1",
            "select": "occurrence:occ/clip:voice", "args": {"from": start, "to": end}}


@pytest.mark.parametrize("same_text", [False, True])
@pytest.mark.parametrize("retained", [True], indirect=True)
def test_undo_recovers_before_binding_even_when_text_writes_are_equal(recipe, same_text):
    case = recipe
    a = pick(case)
    accepted_a = case.submit(case.request([a]), "anchor-a")
    expected = projected(case).timings[TARGET]
    b = pick(case, 1, text='"again"' if same_text else '"again" +1f')
    accepted_b = case.submit(case.request([b]), "anchor-b")
    assert projected(case).timings[TARGET] != expected
    record_b = history(case)[-1]["records"]["commands"][0]
    if same_text:
        write = next(write for write in record_b["writes"] if write["path"] == ["app", "astrid", "on"])
        assert write["before"] == write["after"]
        assert accepted_a["data"]["canonical_document"] != accepted_b["data"]["canonical_document"]
        for name in ("revision_id", "content_digest"):
            assert (accepted_a["data"]["canonical_document"]["parent_revision"][name]
                    != accepted_b["data"]["canonical_document"]["parent_revision"][name])
    # This publication changes derived timing and source order, without writing
    # the target's declaration. Undo B must recover A's choice at the new time.
    moved = capture_move(bundle(case), "occurrence:occ/clip:voice", 8)
    case.submit(case.request([moved]), "move-source-before-undo")
    inverse_b = undo(case, accepted_b, "undo-b")
    case.restart()
    assert authored(bundle(case))[KEY] == a["args"]
    assert projected(case).timings[TARGET]["start"] > expected["start"]
    undo(case, inverse_b, "redo-b")
    assert authored(bundle(case))[KEY] == b["args"]


@pytest.mark.parametrize("retained", [True], indirect=True)
def test_follow_rebases_original_choice_after_source_move_and_reorder(recipe):
    case = recipe
    captured = pick(case, 1, text='"again" in line #2 +1f')
    base = case.head()
    original = bundle(case)
    assert retained_inputs.first_selection(original).source[-1] == "voice"
    moved = capture_move(original, "occurrence:occ/clip:voice", 8)
    case.submit(case.request([moved]), "move-source")
    # Ordinal reselection now points at the other raw clip of the SAME CAS and
    # exact stable word. The command must retain the originally clicked clip.
    assert retained_inputs.first_selection(bundle(case)).source[-1] == "other"
    accepted = case.submit(case.request([captured], base), "stale-anchor")
    assert accepted["data"]["status"] == "rebased"
    assert authored(bundle(case))[KEY] == captured["args"]
    expected = projected(case).timings[TARGET]
    assert expected["start"] > 8
    case.restart()
    assert projected(case).timings[TARGET] == expected
    # A subsequent accepted source move also follows without a direct timing
    # read guard on the declaration's source start/window/order.
    moved = capture_move(bundle(case), "occurrence:occ/clip:voice", 5)
    case.submit(case.request([moved]), "move-again")
    assert projected(case).timings[TARGET]["start"] < expected["start"]
    record = next(row for row in history(case) if row["publication_id"] == accepted["receipt"]["receipt_id"])
    reads = record["records"]["commands"][0]["reads"]
    assert not any(read["ref"].endswith("/clip:voice") and read["path"] == ["at"] for read in reads)


def test_phrase_records_all_consecutive_main_word_ids(recipe):
    case = recipe
    command = pick(case, text='"again again"', count=2)
    case.submit(case.request([command]), "phrase")
    assert len(authored(bundle(case))[KEY]["choice"]["word_ids"]) == 2
    assert projected(case).timings[TARGET]["start_frame"] == 89
    invalid = copy.deepcopy(command)
    invalid["args"]["choice"]["word_ids"].reverse()
    with pytest.raises(CommandError, match="consecutive"):
        commands.execute(bundle(case), [{key: invalid[key] for key in
            ("schema_version", "op", "version", "select", "args", "intent")}])


@pytest.mark.parametrize("retained", [True], indirect=True)
def test_same_word_in_reused_cas_clips_retains_raw_source_through_undo(recipe):
    case = recipe
    a = pick(case, 1)
    accepted_a = case.submit(case.request([a]), "voice-word")
    timing_a = projected(case).timings[TARGET]
    b = pick(case, 1, source="other")
    assert a["args"]["choice"]["document"] == b["args"]["choice"]["document"]
    assert a["args"]["choice"]["word_ids"] == b["args"]["choice"]["word_ids"]
    accepted_b = case.submit(case.request([b]), "other-word")
    for name in ("revision_id", "content_digest"):
        assert (accepted_a["data"]["canonical_document"]["parent_revision"][name]
                != accepted_b["data"]["canonical_document"]["parent_revision"][name])
    timing_b = projected(case).timings[TARGET]
    assert timing_b != timing_a
    assert authored(bundle(case))[KEY]["choice"]["source"] == "occurrence:occ/clip:other"
    undo(case, accepted_b, "undo-other")
    case.restart()
    assert authored(bundle(case))[KEY] == a["args"]
    assert projected(case).timings[TARGET] == timing_a
    restore(case, accepted_b, "restore-other")
    assert projected(case).timings[TARGET] == timing_b
    restore(case, accepted_a, "restore-voice")
    assert projected(case).timings[TARGET] == timing_a


def test_last_write_clear_undo_restore_and_restore_inverse(recipe):
    case = recipe
    a = pick(case)
    accepted_a = case.submit(case.request([a]), "a")
    b = pick(case, 1, text='"again" +1f')
    case.submit(case.request([b]), "b")
    assert authored(bundle(case))[KEY] == b["args"]
    clear = {"schema_version": 1, "op": OP, "version": "1", "select": REF,
             "args": {"field": "on", "declaration": None, "choice": None}}
    cleared = case.submit(case.request([clear]), "clear")
    assert KEY not in authored(bundle(case))
    app = projected(case).bundle["parent"]["clips"][0]["app"]["astrid"]
    assert "on" not in app
    assert "on" not in app["word_anchors"]
    undo(case, cleared, "undo-clear")
    assert authored(bundle(case))[KEY] == b["args"]
    restored = restore(case, accepted_a, "restore-a")
    assert authored(bundle(case))[KEY] == a["args"]
    case.restart()
    undo(case, restored, "undo-restore")
    assert authored(bundle(case))[KEY] == b["args"]


def test_equal_text_references_produce_distinct_canonical_revisions(recipe):
    # A and B deliberately share the same text echo but bind different words.
    # Their authored references, and therefore their canonical revisions, must
    # remain distinct without consulting or folding accepted history.
    case = recipe
    command_a = pick(case)
    a = case.submit(case.request([command_a]), "a")
    command_b = pick(case, 1)
    b = case.submit(case.request([command_b]), "b")
    assert command_a["args"]["declaration"] == command_b["args"]["declaration"] == '"again"'
    assert command_a["args"]["choice"]["word_ids"] != command_b["args"]["choice"]["word_ids"]
    assert a["data"]["canonical_document"] != b["data"]["canonical_document"]
    for name in ("revision_id", "content_digest"):
        assert (a["data"]["canonical_document"]["parent_revision"][name]
                != b["data"]["canonical_document"]["parent_revision"][name])
    assert a["receipt"]["receipt_id"] != b["receipt"]["receipt_id"]


def test_equal_text_restore_recovers_the_selected_publications_binding(recipe):
    case = recipe
    command_a = pick(case)
    a = case.submit(case.request([command_a]), "a")
    command_b = pick(case, 1)
    b = case.submit(case.request([command_b]), "b")
    assert a["data"]["canonical_document"] != b["data"]["canonical_document"]
    restore(case, a, "restore-a")
    assert authored(bundle(case))[KEY] == command_a["args"]
    timing_a = projected(case).timings[TARGET]
    case.restart()
    restore(case, b, "restore-b")
    assert authored(bundle(case))[KEY] == command_b["args"]
    assert projected(case).timings[TARGET] != timing_a
    restore(case, a, "restore-a-again")
    assert projected(case).timings[TARGET] == timing_a


@pytest.mark.parametrize("damage", ["missing", "foreign", "revoked", "invented", "missing-word"])
def test_invalid_inputs_fail_closed_before_acceptance(recipe, damage):
    case = recipe
    call = pick(case)
    current = case.head()
    if damage in ("invented", "missing-word"):
        if damage == "invented":
            call["bindings"]["anchor.source"] = "occurrence:occ/clip:other"
        else:
            call["args"]["choice"]["word_ids"] = ["word:" + "0" * 64]
        from runtime_protocol.errors import RuntimeErrorBase
        with pytest.raises(RuntimeErrorBase) as error:
            case.submit(case.request([call]), damage)
        assert error.value.details["reason"] == ("capture_mismatch" if damage == "invented" else "invalid_anchor")
    else:
        candidate = bundle(case)
        sources = candidate["source_mapping"]["pinned_documents"]
        if damage in ("missing", "revoked"):
            sources.clear()
        else:
            sources[0]["document"]["project_id"] = "foreign-project"
        # Source authority is enforced by the real Runtime port in the retained
        # input neighbor tests. The pure recipe receives only allowed captures.
        with pytest.raises(CommandError, match="missing or revoked|scoped pin"):
            commands.execute(candidate, [call])
    assert case.head() == current


def test_raw_canonical_bundle_compiles_in_fresh_process_without_history(recipe):
    case = recipe
    accepted = case.submit(case.request([pick(case, 1, text='"again" +1f')]), "canonical")
    raw = bundle_from_document(json.loads(json.dumps(accepted["data"]["canonical_document"])))
    sources = copy.deepcopy(case.hydrated[-1]["source_mapping"]["pinned_documents"])
    assert raw["parent"]["clips"][0]["app"]["astrid"]["word_anchors"]["on"]["document"] == case.pin
    # This process receives no Runtime root, accepted records, history or picker
    # command. Both inputs are plain JSON, and no test registry hook is installed.
    # The production execute path resolves this operation through the registered
    # commands.operations() entry; compilation below still consumes only JSON.
    script = '''
import json
import sys
from tests.editing_conformance.word_binding_recipe import compile_authored
from astrid.sdk.timeline.commands import operations
assert "clip.anchor" in operations()
bundle, sources = json.load(sys.stdin)
result = compile_authored(bundle, sources)
assert result.bundle == bundle
interval = result.timings[("parent", "", "card")]
print(json.dumps({key: str(interval[key]) for key in ("start", "end", "start_frame", "end_frame")}))
'''
    completed = subprocess.run([sys.executable, "-c", script], input=json.dumps([raw, sources]),
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "start": "7007/2000", "end": "8007/2000", "start_frame": "105", "end_frame": "120"}
    case.restart()
    assert projected(case).timings[TARGET]["start"] == Fraction(7007, 2000)


@pytest.mark.parametrize("echo", ['"stop" #99 +300f', "obsolete display label", None])
def test_reference_is_authoritative_and_kernel_never_searches_echo(recipe, monkeypatch, echo):
    case = recipe
    case.submit(case.request([pick(case, 1)]), "reference")
    case.restart()
    current = bundle(case)
    sources = current["source_mapping"]["pinned_documents"]
    expected = compile_authored(current, sources)
    if echo is not None:
        case.submit(case.request([{"schema_version": 1, "op": OP, "version": "1", "select": REF,
            "args": {"field": "on", "declaration": echo,
                     "choice": authored(current)[KEY]["choice"]}}]), "change-echo")
        case.restart()
        current = bundle(case)
    apps = mirrored_parent_clip_apps(current)
    if echo is None:
        for app in apps:
            del app["on"]
    else:
        for app in apps:
            app["on"] = echo

    def no_search(*args, **kwargs):
        pytest.fail("a present stable reference must never search for text")

    monkeypatch.setattr(moments, "find_words", no_search)
    result = compile_authored(current, sources)
    assert result.timings == expected.timings
    assert result.bundle == current


@pytest.mark.parametrize("text", ['"again"', '"again" #2', '"again" in line #2'])
def test_legacy_ambiguous_text_refuses_choose_occurrence(recipe, text):
    current = bundle(recipe)
    app = current["parent"]["clips"][0]["app"]["astrid"]
    assert "word_anchors" not in app
    app["on"] = text
    with pytest.raises(TimingCompileError, match="^ambiguous anchor; choose occurrence$"):
        compile_authored(current, current["source_mapping"]["pinned_documents"])


@pytest.mark.parametrize("retained", [True], indirect=True)
def test_legacy_unique_word_in_reused_cas_is_still_ambiguous_without_raw_clip(recipe):
    current = bundle(recipe)
    current["parent"]["clips"][0]["app"]["astrid"]["on"] = '"stop"'
    with pytest.raises(TimingCompileError, match="^ambiguous anchor; choose occurrence$"):
        compile_authored(current, current["source_mapping"]["pinned_documents"])


def test_unique_legacy_word_compiles_without_persisting_a_reference(recipe):
    current = bundle(recipe)
    app = current["parent"]["clips"][0]["app"]["astrid"]
    app["on"] = 'after "stop" -1f'
    before = copy.deepcopy(current)
    result = compile_authored(current, current["source_mapping"]["pinned_documents"])
    assert result.timings[TARGET]["start_frame"] == 148
    assert result.bundle == current == before
    assert "word_anchors" not in app


def test_on_until_clear_preserves_other_reference_and_opaque_fields(recipe):
    case = recipe
    on = pick(case)
    case.submit(case.request([on]), "on")
    until = pick(case, 2, text='after "stop" -1f', field="until")
    accepted = case.submit(case.request([until]), "until")
    expected = dict(projected(case).timings[TARGET])
    assert expected["end_frame"] == 148
    clear = {"schema_version": 1, "op": OP, "version": "1", "select": REF,
             "args": {"field": "until", "declaration": None, "choice": None}}
    cleared = case.submit(case.request([clear]), "clear-until")
    assert authored(bundle(case)) == {KEY: on["args"]}
    assert projected(case).timings[TARGET]["end"] != expected["end"]
    inverse = undo(case, cleared, "undo-clear-until")
    assert authored(bundle(case))[(REF, "until")] == until["args"]
    assert projected(case).timings[TARGET] == expected
    undo(case, inverse, "redo-clear-until")
    assert authored(bundle(case)) == {KEY: on["args"]}
    restore(case, accepted, "restore-both")
    current = bundle(case)
    app = current["parent"]["clips"][0]["app"]["astrid"]
    app["other_extension"] = {"opaque": [None, False, {"value": "kept"}]}
    current["parent"]["clips"][0]["app"]["another.namespace"] = {"unknown": None}
    before = copy.deepcopy(current)
    plan = commands.execute(current, [clear])
    after = plan.bundle["parent"]["clips"][0]["app"]
    assert after["astrid"]["other_extension"] == app["other_extension"]
    assert after["another.namespace"] == {"unknown": None}
    assert after["astrid"]["word_anchors"] == {"on": on["args"]["choice"]}
    assert current == before


def test_half_open_window_and_exact_rational_fps_are_validated_at_acceptance(recipe, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase

    case = recipe
    chosen = pick(case, 2, text='after "stop" -1f')
    case.submit(case.request([chosen]), "end-word")
    # The selected word ends exactly at source.to = 6, which is allowed.
    assert projected(case).timings[TARGET]["start_frame"] == 148
    valid = window_command(monkeypatch, 3, 6)
    accepted = case.submit(case.request([valid]), "narrow-window")
    assert projected(case).timings[TARGET]["start_frame"] == 133
    case.restart()
    assert projected(case).timings[TARGET]["start_frame"] == 133
    head = case.head()
    # Starting at the exclusive source end cannot include this word.
    invalid = copy.deepcopy(valid)
    invalid["args"] = {"from": 2, "to": 5.5}
    with pytest.raises(RuntimeErrorBase, match="outside the half-open source window") as error:
        case.submit(case.request([invalid]), "exclude-word")
    assert error.value.details["reason"] == "invalid_anchor"
    assert error.value.details["status"] == "invalid"
    assert case.head() == head
    undo(case, accepted, "undo-window")
    assert projected(case).timings[TARGET]["start_frame"] == 148


def test_stale_anchor_refuses_when_intervening_window_excludes_selected_word(recipe, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase

    case = recipe
    selected = pick(case, 1)
    base = case.head()
    # No authored reference exists yet, so the fixture can narrow the window.
    case.submit(case.request([window_command(monkeypatch, 4, 6)]), "trim-before-anchor")
    head = case.head()
    with pytest.raises(RuntimeErrorBase, match="outside the half-open source window") as error:
        case.submit(case.request([selected], base), "stale-outside-window")
    assert error.value.details["reason"] == "invalid_anchor"
    assert error.value.details["status"] == "refused"
    assert case.head() == head


@pytest.mark.parametrize("damage,match", [
    ("missing-occurrence", "source occurrence/clip is missing"),
    ("missing-clip", "source occurrence/clip is missing"),
    ("missing-word", "stable word is missing"),
    ("missing-pin", "exact transcript pin is missing"),
    ("wrong-pin", "exact transcript pin is missing"),
    ("malformed-id", "invalid authored word reference"),
    ("empty-ids", "invalid authored word reference"),
    ("duplicate-ids", "invalid authored word reference"),
    ("null-reference", "invalid authored word reference"),
    ("unknown-field", "invalid authored word anchor fields"),
    ("outside-window", "outside the half-open source window"),
    ("nonfinite-offset", "expected finite JSON"),
    ("offset-precision", "exceeds main Moment precision"),
    ("unsupported-owner", "spike requires parent word anchor owner"),
])
def test_invalid_authored_reference_never_falls_back_to_valid_echo(recipe, damage, match):
    case = recipe
    case.submit(case.request([pick(case, 1)]), "chosen")
    current = bundle(case)
    apps = mirrored_parent_clip_apps(current)
    app = apps[0]
    refs = [entry["word_anchors"]["on"] for entry in apps]
    ref = refs[0]
    sources = current["source_mapping"]["pinned_documents"]
    if damage == "missing-occurrence":
        ref["source"] = "occurrence:gone/clip:voice"
    elif damage == "missing-clip":
        for ref in refs:
            ref["source"] = "occurrence:occ/clip:gone"
    elif damage == "missing-word":
        ref["word_ids"] = ["word:" + "0" * 64]
    elif damage == "missing-pin":
        sources.clear()
    elif damage == "wrong-pin":
        ref["document"]["version"] = 2
    elif damage == "malformed-id":
        ref["word_ids"] = ["word:invented"]
    elif damage == "empty-ids":
        ref["word_ids"] = []
    elif damage == "duplicate-ids":
        ref["word_ids"] *= 2
    elif damage == "null-reference":
        app["word_anchors"]["on"] = None
    elif damage == "unknown-field":
        app["word_anchors"]["for"] = ref
    elif damage == "outside-window":
        current["shots"]["shot"]["internal_timeline"]["clips"][0]["to"] = 3
    elif damage == "nonfinite-offset":
        ref["offset_s"] = float("inf")
    elif damage == "offset-precision":
        ref["offset_s"] = 0.0000001
    elif damage == "unsupported-owner":
        current["shots"]["shot"]["internal_timeline"]["clips"][0]["app"] = {
            "astrid": {"word_anchors": {"on": ref}}}
    before = copy.deepcopy(current)
    head = case.head()
    with pytest.raises(TimingCompileError, match=match):
        compile_authored(current, sources)
    assert current == before
    assert case.head() == head


def test_typed_second_offset_preserves_main_precision_at_rational_frame_boundary(recipe):
    case = recipe
    selected = pick(case, 1, text='"again" +1.237399s')
    case.submit(case.request([selected]), "seconds")
    # Generic human Moment formatting uses six significant digits, which would
    # spell +1.2374s and cross main's frame-tolerance boundary. Typed values must not drift.
    result = projected(case).timings[TARGET]
    assert result["start_frame"] == 141
    assert result["start"] == Fraction(141141, 30000)
    assert authored(bundle(case))[KEY]["choice"]["offset_s"] == 1.237399


def test_stale_choice_and_guarded_inverse_refuse_competing_target_writes(recipe):
    from runtime_protocol.errors import RuntimeErrorBase

    case = recipe
    case.submit(case.request([pick(case)]), "a")
    choice_a = authored(bundle(case))[KEY]
    base = case.head()
    stale = pick(case, 2, text='"stop"')
    accepted_b = case.submit(case.request([pick(case, 1)]), "b")
    head = case.head()
    with pytest.raises(RuntimeErrorBase) as error:
        case.submit(case.request([stale], base), "stale-target")
    assert error.value.details["status"] == "conflict"
    assert case.head() == head
    # A later write of the SAME B reference still prevents undoing the older B.
    case.submit(case.request([pick(case, 1)]), "same-b-again")
    head = case.head()
    with pytest.raises(RuntimeErrorBase) as error:
        undo(case, accepted_b, "undo-over-newer-b")
    assert error.value.details["status"] == "conflict"
    assert case.head() == head
    assert authored(bundle(case))[KEY]["choice"]["word_ids"] != choice_a["choice"]["word_ids"]
