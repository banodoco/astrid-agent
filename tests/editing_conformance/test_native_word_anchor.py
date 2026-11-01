"""Native authored word anchors through the actual startup-loaded Runtime provider."""
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
from astrid.sdk.timeline.native_word_anchor import OP, authored, compile_authored
from tests.editing_conformance import native_word_anchor_fixture as retained_inputs

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
    retained.submit(retained.create("recipe-initial"), "recipe-initial")
    return retained


def bundle(case):
    result = bundle_from_document(case.documents[case.head()["revision_id"]])
    # This is the same authorized capture observed from the real provider,
    # handed to the frontend picker, not an inline command/source authority.
    result["source_mapping"]["pinned_documents"] = copy.deepcopy(
        [{"pin": case.pin, "document": case.source}])
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


@pytest.mark.parametrize('retained,match', [
    ({'duration_ms': 100}, 'half-open occurrence interval'),
    ({'source_window': {'from': 20, 'to': 30}}, 'supported source map'),
], indirect=['retained'], ids=['short-occurrence', 'raw-source-window'])
def test_native_acceptance_refuses_review_source_counterexamples_before_writes(recipe, match):
    from runtime_protocol.errors import RuntimeErrorBase

    from astrid.sdk.timeline._recording import Recording
    from astrid.sdk.timeline.native_word_anchor import declare

    case = recipe
    call = {'schema_version': 1, 'op': OP, 'version': '1', 'select': REF,
        'args': {'field': 'on', 'declaration': '"again"', 'choice': {
            'source': 'occurrence:occ/clip:voice', 'document': case.pin,
            'word_ids': [case.source['content']['segments'][0]['words'][1]['id']]}}}
    head = case.service.get_project_timeline(case.project, 'film')
    accepted = history(case)
    with pytest.raises(RuntimeErrorBase, match=match) as error:
        case.submit(case.request([call]), 'review-counterexample')
    assert error.value.details['reason'] == 'invalid_anchor'
    assert error.value.details['status'] == 'invalid'
    assert case.service.get_project_timeline(case.project, 'film') == head
    assert history(case) == accepted
    negative = bundle(case)
    if 'duration_ms' in getattr(case, 'negative_options', {}):
        negative['placements'][0]['duration_ms'] = case.negative_options['duration_ms']
    if 'source_window' in getattr(case, 'negative_options', {}):
        negative['shots']['shot']['internal_timeline']['clips'][0]['source_window'] = copy.deepcopy(
            case.negative_options['source_window'])
    ctx = Recording(negative, call, 0)
    before = copy.deepcopy(ctx.state.bundle)
    with pytest.raises(CommandError, match=match):
        declare(ctx, call)
    assert ctx.record['writes'] == []
    assert ctx.state.bundle == before


@pytest.mark.parametrize('retained', [{'duration_ms': 1750}], indirect=True)
def test_word_ending_at_occurrence_end_preserves_revision_only_restore_and_inverse(recipe):
    case = recipe
    a = case.submit(case.request([pick(case, 1)]), 'boundary-a')
    timing_a = projected(case).timings[TARGET]
    b = case.submit(case.request([pick(case)]), 'boundary-b')
    timing_b = projected(case).timings[TARGET]
    assert timing_a != timing_b
    undo(case, b, 'boundary-undo-b')
    assert projected(case).timings[TARGET] == timing_a
    case.restart()
    restore(case, b, 'boundary-restore-b')
    assert projected(case).timings[TARGET] == timing_b
    restore(case, a, 'boundary-restore-a')
    assert projected(case).timings[TARGET] == timing_a


def test_occurrence_window_is_rechecked_for_source_move_stale_replay_and_inverse(recipe):
    from runtime_protocol.errors import RuntimeErrorBase

    case = recipe
    selected = pick(case, 1)
    anchored = case.submit(case.request([selected]), 'window-anchor')
    move = {'schema_version': 1, 'op': 'clip.move', 'version': '1',
            'select': 'occurrence:occ/clip:voice', 'args': {'on': 15}}
    head = case.head()
    with pytest.raises(RuntimeErrorBase, match='half-open occurrence interval'):
        case.submit(case.request([move]), 'outside-with-live-anchor')
    assert case.head() == head
    undo(case, anchored, 'remove-live-anchor')
    selected = pick(case, 1)
    base = case.head()
    case.submit(case.request([move]), 'outside-without-anchor')
    head = case.head()
    with pytest.raises(RuntimeErrorBase, match='half-open occurrence interval'):
        case.submit(case.request([selected], base), 'stale-outside-occurrence')
    assert case.head() == head
    # Restore recovers the whole immutable revision, including its valid source.
    restore(case, anchored, 'restore-original-source-and-anchor')
    assert authored(bundle(case))[KEY] == selected['args']
    clear = {'schema_version': 1, 'op': OP, 'version': '1', 'select': REF,
             'args': {'field': 'on', 'declaration': None, 'choice': None}}
    cleared = case.submit(case.request([clear]), 'clear-for-inverse')
    case.submit(case.request([move]), 'outside-before-inverse')
    head = case.head()
    with pytest.raises(RuntimeErrorBase, match='half-open occurrence interval'):
        undo(case, cleared, 'inverse-outside-occurrence')
    assert case.head() == head


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
    assert accepted["data"]["status"] == "merged"
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
    # command. Both inputs are plain JSON, and no registry hook is installed.
    script = '''
import json
import sys
from astrid.sdk.timeline.native_word_anchor import compile_authored
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
    apps = mirrored_parent_clip_apps(current)
    for app in apps:
        app["on"] = 'after "stop" -1f'
    before = copy.deepcopy(current)
    result = compile_authored(current, current["source_mapping"]["pinned_documents"])
    assert result.timings[TARGET]["start_frame"] == 148
    assert result.bundle == current == before
    assert all("word_anchors" not in app for app in apps)


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
    assert error.value.details["status"] == "conflict"
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
    ("unsupported-owner", "word anchors require parent ownership"),
])
def test_invalid_authored_reference_never_falls_back_to_valid_echo(recipe, damage, match):
    case = recipe
    case.submit(case.request([pick(case, 1)]), "chosen")
    current = bundle(case)
    app = current["parent"]["clips"][0]["app"]["astrid"]
    ref = app["word_anchors"]["on"]
    sources = current["source_mapping"]["pinned_documents"]
    if damage == "missing-occurrence":
        ref["source"] = "occurrence:gone/clip:voice"
    elif damage == "missing-clip":
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


def test_first_use_retains_v1_before_producer_removes_words_and_exact_retry_after_restart(recipe):
    from astrid.sdk.media_transcript import build_transcript

    case = recipe
    initial = case.documents[case.head()['revision_id']]
    assert initial['dependency_manifest']['documents'] == [case.pin]
    request = case.request([pick(case, 1)])
    accepted = case.submit(request, 'first-use')
    canonical = accepted['data']['canonical_document']
    assert canonical['dependency_manifest']['documents'] == [case.pin]
    clip = canonical['parent_revision']['payload']['clips'][0]
    expected = projected(case).timings[TARGET]
    assert clip['at'] == float(expected['start'])
    assert clip['hold'] == float(expected['duration'])
    replacement = build_transcript(case.source['content']['media_id'], 'line', 'stop', [[5.5, 6, 'stop']])
    current = case.service.update_document(case.project, case.pin['document_id'],
        {'expected_version': 1, 'content': replacement}, idempotency_key='producer-v2')['data']
    assert current['version'] == 2
    assert request['commands'][0]['args']['choice']['word_ids'][0] not in {
        word['id'] for segment in current['content']['segments'] for word in segment['words']}
    case.restart()
    assert case.submit(request, 'first-use') == accepted
    reloaded = case.submit(case.create('after-v2'), 'after-v2')
    assert reloaded['data']['canonical_document']['dependency_manifest']['documents'] == [case.pin]
    assert case.hydrated[-1]['source_mapping']['pinned_documents'] == [
        {'pin': case.pin, 'document': case.source}]
    assert projected(case).timings[TARGET] == expected


def test_native_move_refuses_anchor_basis_until_explicit_clear(recipe):
    case = recipe
    case.submit(case.request([pick(case, 1)]), 'anchor')
    current = bundle(case)
    before = copy.deepcopy(current)
    with pytest.raises(CommandError, match='clear word anchors before changing the timing basis'):
        capture_move(current, REF, 8)
    assert current == before
    clear = {'schema_version': 1, 'op': OP, 'version': '1', 'select': REF,
             'args': {'field': 'on', 'declaration': None, 'choice': None}}
    case.submit(case.request([clear]), 'clear')
    moved = capture_move(bundle(case), REF, 8)
    accepted = case.submit(case.request([moved]), 'absolute-move')
    assert accepted['data']['canonical_document']['parent_revision']['payload']['clips'][0]['at'] > 7.9
    assert not authored(bundle(case))


def test_source_removal_and_changed_media_refuse_atomically(recipe, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase

    case = recipe
    case.submit(case.request([pick(case, 1)]), 'anchor')
    current = case.head()
    registry = commands.operations

    def damage(ctx, call):
        if call['args']['remove']:
            ctx.clear(call['select'], [])
        else:
            ctx.write('occurrence:occ/registry', ['assets', 'take', 'media_id'], 'sha256:' + 'f' * 64)

    monkeypatch.setattr(commands, 'operations', lambda: {**registry(), 'fixture.source': commands.Operation(
        {'type': 'object', 'properties': {'remove': {'type': 'boolean'}}}, damage, {}, True)})
    for remove in (False, True):
        with pytest.raises(RuntimeErrorBase, match='bound media source|source occurrence/clip is missing'):
            case.submit(case.request([{'schema_version': 1, 'op': 'fixture.source', 'version': '1',
                'select': 'occurrence:occ/clip:voice', 'args': {'remove': remove}}]), 'damage-' + str(remove))
        assert case.head() == current


def test_unrelated_legacy_timing_is_outside_native_anchor_slice(recipe):
    current = bundle(recipe)
    current['parent']['clips'].append({'id': 'legacy', 'clipType': 'am-type', 'track': 'picture',
        'at': 0, 'hold': 1, 'app': {'astrid': {'until_film_end': True, 'sequence': 'old'}}})
    original = copy.deepcopy(current['parent']['clips'][-1])
    plan = commands.execute(current, [pick(recipe, 1)])
    assert plan.bundle['parent']['clips'][-1] == original


def test_first_anchor_inverse_recovers_literal_timing_and_exact_absence(recipe):
    case = recipe
    before = copy.deepcopy(bundle(case)['parent']['clips'][0])
    accepted = case.submit(case.request([pick(case, 1)]), 'first-anchor')
    inverted = undo(case, accepted, 'undo-first-anchor')
    case.restart()
    assert bundle(case)['parent']['clips'][0] == before
    redone = undo(case, inverted, 'redo-first-anchor')
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']


def test_first_anchor_redo_rejects_corrupt_derived_history(recipe):
    from astrid.sdk.timeline._step_undo import validate_nested_history

    case = recipe
    accepted = case.submit(case.request([pick(case, 1)]), 'anchor-history')
    undo(case, accepted, 'undo-anchor-history')
    entries = {entry['publication_id']: entry for entry in history(case)}
    original = entries[case.head()['publication_id']]
    validate_nested_history(original, entries.__getitem__)
    for damage in ('before', 'after', 'path', 'cause', 'missing'):
        corrupted = copy.deepcopy(original)
        writes = corrupted['records']['commands'][0]['derived_writes']
        timing = next(write for write in writes
                      if write['ref'] == REF and write['path'] == ['at'])
        if damage in ('before', 'after'):
            timing[damage] = {'state': 'present', 'value': 99}
        elif damage == 'path':
            timing['path'] = ['opacity']
        elif damage == 'cause':
            timing['cause'] = 99
        else:
            writes.remove(timing)
        with pytest.raises(CommandError):
            validate_nested_history(corrupted, entries.__getitem__)


def test_anchor_inverse_rejects_forged_timing_and_matching_effects(recipe, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    from astrid.core.timeline.authoring_bundle import _digest

    case = recipe
    case.submit(case.request([pick(case)]), 'prior-anchor')
    accepted = case.submit(case.request([pick(case, 1)]), 'changed-anchor')
    head, retained_history = case.head(), history(case)
    accept = RequestCommitPort.accept
    reached = []

    def forged(port, prepared):
        value = copy.deepcopy(prepared)
        record = value['records']['commands'][0]
        timing = next(write for write in record['derived_writes']
                      if write['ref'] == REF and write['path'] == ['at'])
        timing['after']['value'] = 99
        for step in value['records']['steps']:
            for effect in step['patch']:
                if effect['path'][-1:] == ['at']:
                    effect['value'] = 99
        document = value['canonical_document']
        parent = document['parent_revision']
        for clips in (parent['payload']['clips'], parent['payload']['config']['clips']):
            next(clip for clip in clips if clip['id'] == 'card')['at'] = 99
        parent['content_digest'] = _digest(parent['payload'])
        parent['revision_id'] = 'forged-anchor-inverse-timing'
        value['document_digest'] = _digest(document)
        reached.append(True)
        return accept(port, value)

    monkeypatch.setattr(RequestCommitPort, 'accept', forged)
    with pytest.raises(RuntimeErrorBase, match='sequential execution capture does not bind') as error:
        undo(case, accepted, 'forged-anchor-inverse')
    assert error.value.details['reason'] == 'invalid_records'
    assert reached == [True]
    assert case.head() == head
    assert history(case) == retained_history


@pytest.mark.parametrize('damage', ['missing-version', 'wrong-digest', 'foreign-document'])
def test_provider_resolves_exact_first_use_pin_and_refuses_unavailable_source(recipe, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    case = recipe
    call = pick(case, 1)
    pin = call['args']['choice']['document']
    if damage == 'missing-version':
        pin['version'] = 99
    elif damage == 'wrong-digest':
        pin['content_digest'] = 'sha256:' + '0' * 64
    else:
        pin['document_id'] = 'transcript:' + '0' * 64
    head = case.head()
    with pytest.raises(RuntimeErrorBase):
        case.submit(case.request([call]), 'unavailable-' + damage)
    assert case.head() == head
    assert case.documents[head['revision_id']]['dependency_manifest']['documents'] == [case.pin]


def test_source_read_revocation_is_atomic_before_first_use_retention(recipe, monkeypatch):
    from runtime_protocol.errors import AuthorizationError
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    case = recipe
    call = pick(case, 1)
    current = case.head()
    allowed = True
    source = RequestCommitPort.transcript_source

    def revoke(port, pin):
        nonlocal allowed
        allowed = False
        return source(port, pin)

    def authorize():
        if not allowed:
            raise AuthorizationError('source authority revoked')

    monkeypatch.setattr(RequestCommitPort, 'transcript_source', revoke)
    with pytest.raises(AuthorizationError, match='source authority revoked'):
        case.submit(case.request([call]), 'revoked-first-use', authorize=authorize)
    assert case.head() == current
    assert case.documents[current['revision_id']]['dependency_manifest']['documents'] == [case.pin]
