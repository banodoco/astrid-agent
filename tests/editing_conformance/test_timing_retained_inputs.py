"""Real retained inputs, and the missing accepted word-binding seam.

The native fixture seeds a bounded film through the current Runtime commit
provider and retains its exact transcript input. Measured commands use the
installed Agent. First-selection below is a bounded experiment, NOT a history
adapter or a new authoring representation.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

import pytest

from astrid.core.timeline import moments
from astrid.core.timeline.authoring_bundle import _digest
from astrid.sdk.media_transcript import build_transcript, transcript_document_id
from astrid.sdk.timeline import commit as agent
from astrid.sdk.timeline.capture import capture_move, capture_track_create
from astrid.sdk.timeline.compiler import (
    TimingCompileError,
    TimingInputs,
    WordBinding,
    compile_timing,
)
from astrid.sdk.timeline.units import source_to_timeline_time
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration
SOURCE = ("occ", "shot", "voice")
TARGET = ("parent", "", "card")
DECLARATION = '"again" in line #2 +1f'
FPS = {"numerator": 30000, "denominator": 1001}


def first_selection(bundle):
    """Select once from this fixture's sole verified source using main's grammar.

    No binding is accepted from a caller. This intentionally has no replay
    mode: the tests expose why re-running selection is insufficient for replay.
    """
    captures = bundle["source_mapping"]["pinned_documents"]
    assert len(captures) == 1
    capture = captures[0]
    pin, document = capture["pin"], capture["document"]
    declaration = bundle["parent"]["clips"][0]["app"]["astrid"]["on"]
    moment = moments.parse(declaration)
    words = []
    for row in bundle["placements"]:
        assert row["source_offset"] == 0 and row["speed"] == 1
        internal = bundle["shots"][row["shot_id"]]["internal_timeline"]
        for clip in internal["clips"]:
            asset = internal["registry"]["assets"][clip["asset"]]
            assert asset["media_id"] == document["content"]["media_id"]
            owner = (row["occurrence_id"], row["shot_id"], clip["id"])
            origin = Fraction(row["placement"]["start_ms"], 1000) + Fraction(str(clip["at"]))
            for segment in document["content"]["segments"]:
                for index, word in enumerate(segment["words"]):
                    if not clip["from"] <= word["start"] < word["end"] <= clip["to"]:
                        continue
                    times = {key: source_to_timeline_time(Fraction(str(word[key])),
                        source_start=Fraction(str(clip["from"])), timeline_start=origin,
                        speed=clip["speed"]) for key in ("start", "end")}
                    words.append(SimpleNamespace(**dict(word, **times), segment=segment["id"],
                                                 index=index, owner=owner))
    words.sort(key=lambda word: (word.start, word.owner, word.id))
    hits = moments.find_words(moment, words)
    assert moment.n is not None or len(hits) == 1
    chosen = hits[(moment.n or 1) - 1]
    assert len({word.owner for word in chosen}) == 1
    return WordBinding(TARGET, "on", declaration, chosen[0].owner, pin["document_id"],
                       pin["version"], pin["content_digest"], tuple(word.id for word in chosen))


@pytest.fixture
def retained(tmp_path, monkeypatch, request):
    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import route_path

    from tests.editing_conformance.native_word_anchor_fixture import seed

    root = tmp_path / "realm"
    options = getattr(request, "param", False)
    options = options if isinstance(options, dict) else {"reused": options}
    seeded = seed(root, **options)
    project, pin, source = (seeded[key] for key in ("project", "pin", "source"))
    service = RuntimeService(root)

    case = SimpleNamespace(service=service, root=root, project=project, pin=pin, source=source,
                           hydrated=[], documents={})

    def head():
        value = case.service.get_project_timeline(project, "film")
        return {"revision_id": value["head_revision_id"], "publication_id": value["head_publication_id"]}

    def request(commands, base=None):
        base = base or head()
        return {"api_version": 1, "project_id": project, "timeline_id": "film",
                "base_revision_id": base["revision_id"], "base_publication_id": base["publication_id"],
                "commands": commands}

    def submit(body, key, authorize=lambda: None):
        result = case.service.commit_timeline(project, "film", body, idempotency_key=key,
            publisher="timing-editor", invocation_path=route_path(project, "film"), authorize=authorize)
        document = result["data"]["canonical_document"]
        case.documents[document["parent_revision"]["revision_id"]] = document
        return result

    case.service.register_timeline_commit_provider(agent.create_runtime_commit_service())
    hydrate = agent._hydrate

    def observed(port, bundle, calls=(), **kwargs):
        result = hydrate(port, bundle, calls, **kwargs)
        case.hydrated.append(copy.deepcopy(bundle))
        return result

    monkeypatch.setattr(agent, "_hydrate", observed)

    case.seed = submit(request([{"schema_version": 1, "op": "track.create", "version": "1",
        "select": "parent", "created": ["parent/track:initial"],
        "args": {"row": {"id": "initial", "kind": "audio"}}}]), "initial")

    # The current native fixture starts with an empty document manifest. Use
    # the real parent anchor carrier once to retain this exact transcript pin,
    # then explicitly clear the authored choice before the measured commands.
    # This keeps source custody in Runtime history without inventing a binding
    # for any of the retained-input cases below.
    from astrid.sdk.timeline.native_word_anchor import capture_anchor

    anchor_bundle = agent.bundle_from_document(case.seed["data"]["canonical_document"])
    anchor_bundle["source_mapping"]["pinned_documents"] = [
        {"pin": copy.deepcopy(pin), "document": copy.deepcopy(source)}]
    choice = {"source": "occurrence:occ/clip:voice", "document": copy.deepcopy(pin),
              "word_ids": [source["content"]["segments"][0]["words"][1]["id"]],
              "offset_frames": 1}
    anchored = capture_anchor(anchor_bundle, "parent/clip:card", "on", DECLARATION, choice)
    case.submit(request([anchored]), "retain-transcript-pin")
    clear_bundle = agent.bundle_from_document(case.documents[case.head()["revision_id"]])
    cleared = capture_anchor(clear_bundle, "parent/clip:card", "on", None, None)
    case.submit(request([cleared]), "clear-retained-word-choice")

    def restart(provider=None):
        case.service.close()
        case.service = RuntimeService(root)
        case.service.register_timeline_commit_provider(provider or agent.create_runtime_commit_service())

    def create(track, base=None):
        base = base or head()
        bundle = agent.bundle_from_document(case.documents[base["revision_id"]])
        command = capture_track_create(bundle, "parent", {"id": track, "kind": "audio"})
        return request([command], base)

    case.head, case.request, case.submit, case.restart, case.create = head, request, submit, restart, create
    try:
        yield case
    finally:
        case.service.close()


def test_exact_retained_v1_compiles_after_cas_v2_restart_and_cache_corruption(retained):
    case = retained
    original = case.submit(case.create("first"), "first")
    bundle = case.hydrated[-1]
    binding = first_selection(bundle)
    assert binding.word_ids == (case.source["content"]["segments"][0]["words"][1]["id"],)
    inputs = TimingInputs(bundle, sources=bundle["source_mapping"]["pinned_documents"], bindings=[binding])
    expected = compile_timing(inputs)
    assert expected.timings[TARGET]["start"] == Fraction(7007, 2000)
    assert expected.timings[TARGET]["end"] == Fraction(8007, 2000)
    assert (expected.timings[TARGET]["start_frame"], expected.timings[TARGET]["end_frame"]) == (105, 120)
    assert (expected.timings[SOURCE]["start"], expected.timings[SOURCE]["end"]) == (3, 5)

    replacement = build_transcript(case.source["content"]["media_id"], "line", "again again stop", [
        [2.5, 3, "again"], [4, 4.5, "again"], [5.5, 6, "stop"],
    ])
    current = case.service.update_document(case.project, case.pin["document_id"],
        {"expected_version": 1, "content": replacement}, idempotency_key="producer-v2")["data"]
    assert current["version"] == 2 and current["content"] != case.source["content"]
    case.service.close()
    # Fresh interpreter, no in-memory TimingInputs/WordBinding handed to it.
    # This proves exact source recovery and first selection, not history binding.
    script = '''
import json
import sys
from tests.helpers.runtime import use_runtime
use_runtime()
from runtime_protocol.service import RuntimeService
from runtime_protocol.timeline_commit_port import route_path
from astrid.sdk.timeline import commit as agent
from astrid.sdk.timeline.compiler import TimingInputs, compile_timing
from tests.editing_conformance.test_timing_retained_inputs import first_selection, TARGET
service = RuntimeService(sys.argv[1])
try:
    service.register_timeline_commit_provider(agent.create_runtime_commit_service())
    captured = []
    hydrate = agent._hydrate
    def observe(port, bundle, calls=(), **kwargs):
        result = hydrate(port, bundle, calls, **kwargs)
        captured.append(bundle)
        return result
    agent._hydrate = observe
    project = sys.argv[2]
    head = service.get_project_timeline(project, "film")
    service.commit_timeline(project, "film", {
        "api_version": 1, "project_id": project, "timeline_id": "film", "dry_run": True,
        "base_revision_id": head["head_revision_id"],
        "base_publication_id": head["head_publication_id"],
        "commands": [{"schema_version": 1, "op": "track.create", "version": "1",
            "select": "parent", "args": {"row": {"id": "fresh-preview", "kind": "audio"}}}]},
        idempotency_key="fresh-preview", publisher="timing-editor",
        invocation_path=route_path(project, "film"), authorize=lambda: None)
    bundle = captured[-1]
    sources = bundle["source_mapping"]["pinned_documents"]
    result = compile_timing(TimingInputs(bundle, sources=sources, bindings=[first_selection(bundle)]))
    print(json.dumps({"sources": sources, "start": str(result.timings[TARGET]["start"])}))
finally:
    service.close()
'''
    reopened = subprocess.run([sys.executable, "-c", script, str(case.root), case.project],
                              text=True, capture_output=True, timeout=60)
    assert reopened.returncode == 0, reopened.stderr
    assert json.loads(reopened.stdout) == {
        "sources": [{"pin": case.pin, "document": case.source}], "start": "7007/2000"}
    case.restart()
    case.submit(case.create("after-restart"), "after-restart")
    bundle = case.hydrated[-1]
    assert bundle["source_mapping"]["pinned_documents"] == [{"pin": case.pin, "document": case.source}]
    assert case.service.get_document(case.project, case.pin["document_id"])["version"] == 2
    bundle["parent"]["clips"][0]["at"] = 99  # detached compiler probe, not an accepted mutation
    result = compile_timing(TimingInputs(bundle, sources=bundle["source_mapping"]["pinned_documents"],
                                       bindings=[binding]))
    assert result.timings == expected.timings
    assert result.bundle["parent"]["clips"][0]["at"] == 99
    assert compile_timing(inputs) == expected
    assert original["data"]["canonical_document"]["dependency_manifest"]["documents"] == [case.pin]


def test_accepted_rebase_has_exact_document_but_no_reconstructible_word_choice(retained):
    case = retained
    base = case.head()
    stale = case.create("stale", base)
    bundle = agent.bundle_from_document(case.documents[base["revision_id"]])
    move = capture_move(bundle, "occurrence:occ/clip:voice", 4)
    case.submit(case.request([move], base), "intervening")
    rebased = case.submit(stale, "stale")
    assert rebased["data"]["status"] == "rebased"
    case.restart()
    history = case.service.list_timeline_commits(case.project, "film", authorize=lambda: None)["items"]
    entry = next(row for row in history if row["publication_id"] == rebased["receipt"]["receipt_id"])
    record = entry["records"]["commands"][0]
    assert record["dependencies"] == [{"key": "compile.document:" + _digest(case.pin),
        "kind": "transcript", "pin": case.pin, "policy": "exact"}]
    assert record["bindings"] == []  # track.create never selected a word
    bundle = case.hydrated[-1]
    assert bundle["parent"]["clips"][0]["app"]["astrid"]["on"] == DECLARATION
    with pytest.raises(TimingCompileError, match="missing exact word binding"):
        compile_timing(TimingInputs(bundle, sources=bundle["source_mapping"]["pinned_documents"]))
    # Caller-invented source binding cannot fill the accepted-history gap.
    invented = case.create("invented")
    invented["commands"][0]["bindings"] = {"timing.on.source": "occurrence:occ/clip:voice"}
    from runtime_protocol.errors import RuntimeErrorBase
    with pytest.raises(RuntimeErrorBase) as error:
        case.submit(invented, "invented")
    assert error.value.details["reason"] == "capture_mismatch"


@pytest.mark.parametrize("retained", [True], indirect=True)
def test_reselecting_ordinal_after_real_rebase_silently_changes_raw_source_clip(retained):
    case = retained
    case.submit(case.create("initial"), "initial")
    original = first_selection(case.hydrated[-1])
    assert original.source == SOURCE
    base = case.head()
    stale = case.create("stale", base)
    bundle = agent.bundle_from_document(case.documents[base["revision_id"]])
    move = capture_move(bundle, "occurrence:occ/clip:voice", 8)
    case.submit(case.request([move], base), "move-source-after-other")
    accepted = case.submit(stale, "stale")
    assert accepted["data"]["status"] == "rebased"
    head_bundle = case.hydrated[-1]
    reselected = first_selection(head_bundle)
    # Same declaration, transcript version, digest AND word ID. Only the raw
    # clip occurrence changed. Current-head text/#n lookup cannot recover intent.
    assert reselected.declaration == original.declaration
    assert reselected.word_ids == original.word_ids
    assert reselected.document_id == original.document_id
    assert reselected.content_digest == original.content_digest
    assert reselected.source == ("occ", "shot", "other")
    sources = head_bundle["source_mapping"]["pinned_documents"]
    stable = compile_timing(TimingInputs(head_bundle, sources=sources, bindings=[original]))
    retargeted = compile_timing(TimingInputs(head_bundle, sources=sources, bindings=[reselected]))
    assert stable.timings[TARGET]["start"] > retargeted.timings[TARGET]["end"]
    case.restart()
    history = case.service.list_timeline_commits(case.project, "film", authorize=lambda: None)["items"]
    record = history[-1]["records"]["commands"][0]
    assert record["bindings"] == []
    assert record["dependencies"][0]["pin"] == case.pin


@pytest.mark.parametrize("damage", ["unavailable", "missing-pin", "foreign"])
def test_source_authority_refuses_without_latest_fallback(retained, damage):
    case = retained
    from runtime_protocol.errors import AuthorizationError, NotFoundError

    case.submit(case.create("capture"), "capture")
    bundle = case.hydrated[-1]
    binding = first_selection(bundle)
    sources = copy.deepcopy(bundle["source_mapping"]["pinned_documents"])
    missing = dict(case.pin, version=99) if damage == "missing-pin" else case.pin
    if damage != "foreign":
        sources = []
    else:
        foreign = case.service.create_project({"slug": "foreign", "name": "Foreign"})["id"]
        media = case.service.ingest(foreign, b"timing-source", media_type="audio/wav",
            original_name="voice.wav", idempotency_key="foreign-voice")["data"]["object_id"]
        content = case.source["content"]
        assert media == content["media_id"]
        document_id = transcript_document_id(foreign, media, content["source_id"])
        source = case.service.create_document(foreign, {"document_id": document_id,
            "kind": "media.transcript", "content": content}, idempotency_key="foreign-transcript")["data"]
        missing = {"document_id": document_id, "version": 1, "content_digest": _digest(content)}
        sources = [{"pin": missing, "document": source}]
    with pytest.raises(TimingCompileError, match="missing or revoked|scoped pin"):
        compile_timing(TimingInputs(bundle, sources=sources, bindings=[binding]))
    allowed = True

    def authorize():
        if not allowed:
            raise AuthorizationError("source authority revoked")

    # Refuse at the actual source read, after successfully opening the closure.
    class ReadUnavailable:
        provider_id = "astrid-v1"
        interface_version = 1
        contract_digest = agent.create_runtime_commit_service().contract_digest

        def commit(self, context, port):
            nonlocal allowed
            port.revision_closure(port.head()["revision_id"])
            if damage == "unavailable":
                allowed = False
            port.transcript_source(context["request"]["commands"][0]["args"]["pin"])
            raise AssertionError("unavailable source was returned")

    case.restart(ReadUnavailable())
    request = case.request([{"schema_version": 1, "op": "fixture.read", "version": "1",
        "select": None, "args": {"pin": missing}, "created": []}])
    current = case.head()
    error_type, message = {
        "unavailable": (AuthorizationError, "source authority revoked"),
        "missing-pin": (NotFoundError, "exact document version is not retained"),
        "foreign": (NotFoundError, "document input is not available in the project"),
    }[damage]
    with pytest.raises(error_type, match=message):
        case.submit(request, "missing-source", authorize=authorize)
    assert case.head() == current


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("retained", [True], indirect=True)
def test_retained_v1_and_explicit_v2_keep_window_and_raw_source_identity(retained, version):
    case = retained
    case.submit(case.create("version-comparison"), "version-comparison")
    bundle = case.hydrated[-1]
    original = first_selection(bundle)
    replacement = build_transcript(case.source["content"]["media_id"], "line", "again again stop", [
        [2.5, 3, "again"], [4, 4.5, "again"], [5.5, 6, "stop"],
    ])
    current = case.service.update_document(case.project, case.pin["document_id"],
        {"expected_version": 1, "content": replacement}, idempotency_key="comparison-v2")["data"]
    new_pin = dict(case.pin, version=2, content_digest=_digest(replacement))
    sources = [*bundle["source_mapping"]["pinned_documents"], {"pin": new_pin, "document": current}]
    new_binding = replace(original, version=2, content_digest=new_pin["content_digest"],
                          word_ids=(replacement["segments"][0]["words"][1]["id"],))
    assert new_binding.word_ids != original.word_ids
    selected = original if version == 1 else new_binding
    expected_frame = 105 if version == 1 else 120
    before = copy.deepcopy(bundle)

    def project(candidate, binding=selected, captures=sources):
        return compile_timing(TimingInputs(candidate, sources=captures, bindings=[binding]))

    # Both exact versions may be captured together; neither is treated as latest.
    assert project(bundle).timings[TARGET]["start_frame"] == expected_frame
    with pytest.raises(TimingCompileError, match="missing or revoked"):
        project(bundle, captures=[sources[1 if version == 1 else 0]])
    # Same text and CAS words in another raw clip do not change the chosen owner.
    retargeted = replace(selected, source=("occ", "shot", "other"))
    assert project(bundle, retargeted).timings[TARGET]["start_frame"] == expected_frame + 90
    assert project(bundle).timings[TARGET]["start_frame"] == expected_frame

    # These are detached compiler probes; accepted data still keeps its own window.
    narrowed = copy.deepcopy(bundle)
    voice = next(clip for clip in narrowed["shots"]["shot"]["internal_timeline"]["clips"]
                 if clip["id"] == "voice")
    voice["from"] = 3
    assert project(narrowed).timings[TARGET]["start_frame"] == expected_frame - 15
    voice["to"] = 3.5
    if version == 1:
        # The V1 selected word ends at 3.5, inside [3, 3.5).
        assert project(narrowed).timings[TARGET]["start_frame"] == 90
    else:
        with pytest.raises(TimingCompileError, match="half-open source window"):
            project(narrowed)
    voice["to"] = 3.25
    with pytest.raises(TimingCompileError, match="half-open source window"):
        project(narrowed)
    assert bundle == before
    case.restart()
    case.submit(case.create("retained-after-v2"), "retained-after-v2")
    assert case.hydrated[-1]["source_mapping"]["pinned_documents"] == [sources[0]]
    assert project(case.hydrated[-1]).timings[TARGET]["start_frame"] == expected_frame
