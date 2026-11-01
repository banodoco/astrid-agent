"""Real Runtime fixture using first-use transcript capture by the native provider."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.timeline import moments
from astrid.core.timeline.authoring_bundle import _digest
from astrid.sdk.media_transcript import build_transcript, transcript_document_id
from astrid.sdk.timeline import commit as agent
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.compiler import WordBinding
from astrid.sdk.timeline.units import source_to_timeline_time
from tests.helpers.runtime import use_runtime

TARGET = ('parent', '', 'card')
DECLARATION = '\"again\" in line #2 +1f'
FPS = {'numerator': 30000, 'denominator': 1001}


def _steps(commands, label):
    return [{
        "id": f"{label}-{index + 1}", "label": label, "intent": "", "tags": [],
        "command_ordinals": [index],
        "patch": copy.deepcopy(command.get("args", {}).get("changes", []))
        if command["op"] == "doc.patch" else [],
        "reads": [],
    } for index, command in enumerate(commands)]


def _merge_mode(commands):
    return "atomic" if any(command["op"] == "doc.patch" for command in commands) else "fields"


def first_selection(bundle, declaration=DECLARATION):
    """Select once from this fixture's sole verified source using main's grammar.

    No binding is accepted from a caller. This intentionally has no replay
    mode: the tests expose why re-running selection is insufficient for replay.
    """
    captures = bundle["source_mapping"]["pinned_documents"]
    assert len(captures) == 1
    capture = captures[0]
    pin, document = capture["pin"], capture["document"]
    # The current fixture starts with literal timing and no authored anchor.
    # Reselection compares the requested picker declaration at each source head.
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


def seed(root, reused, *, duration_ms=10000, source_window=None, opaque_pin_scope=None,
         current_form=False):
    # Source documents are ordinary Runtime data.  The timeline fixture is
    # bootstrapped through the live commit provider below, not the retired
    # parent-composition publisher.
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import route_path

    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.emitter import emit_document_commands
    from astrid.sdk.timeline.native_initialize import capture_initialize
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from astrid.sdk.timeline.native_word_anchor import capture_anchor

    root = Path(root)
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    project = service.create_project({"slug": "timing", "name": "Timing"})["id"]
    media = service.ingest(project, b"timing-source", media_type="audio/wav",
                           original_name="voice.wav", idempotency_key="voice")["data"]["object_id"]
    content = build_transcript(media, "line", "again again stop", [
        [2, 2.5, "again"], [3, 3.5, "again"], [5.5, 6, "stop"],
    ])
    document_id = transcript_document_id(project, media, content["source_id"])
    source = service.create_document(project, {"document_id": document_id,
        "kind": "media.transcript", "content": content}, idempotency_key="transcript-v1")["data"]
    pin = {"document_id": document_id, "version": 1, "content_digest": _digest(content)}
    opaque = {"opaque_transcript": {"pin": pin, "raw": [None, False, 0, 0.0, {}, [], ""]}}

    service.register_timeline_commit_provider(agent.create_runtime_commit_service())
    service.create_timeline(project, "film", idempotency_key="timeline")

    def head():
        value = service.get_project_timeline(project, "film")
        return {"revision_id": value["head_revision_id"],
                "publication_id": value["head_publication_id"]}

    def submit(commands, key):
        current = head()
        body = {
            "api_version": 1, "project_id": project, "timeline_id": "film",
            "base_revision_id": current["revision_id"],
            "base_publication_id": current["publication_id"],
            "commands": commands, "label": key, "steps": _steps(commands, key),
            "merge": _merge_mode(commands), "reads": [], "dry_run": False,
            "source": {"via": "python"}, "rationale": key, "group_id": key,
        }
        result = service.commit_timeline(
            project, "film", body, idempotency_key=key, publisher="timing-editor",
            invocation_path=route_path(project, "film"), authorize=lambda: None,
        )
        return result["data"]["canonical_document"]

    bootstrap = service.get_project_parent_composition_revision(
        project, "film", head()["revision_id"])
    empty = {
        "schema_version": 1, "project_id": project, "timeline_id": "film",
        "parent_revision": {key: bootstrap[key] for key in
                             ("revision_id", "content_digest", "payload")},
        "shot_revisions": [], "internal_timeline_revisions": [],
        "dependency_manifest": bootstrap["dependency_manifest"],
    }
    bundle = bundle_from_document(empty)
    initialize = capture_initialize(
        bundle, name="Voice", slug="film", output={
            "resolution": "1280x720", "fps": 30, "file": "output.mp4",
            "background": None, "background_scale": None,
        },
    )
    initialized = execute(bundle, [initialize]).bundle
    parent_track = capture_track_create(initialized, "parent", {
        "id": "picture", "kind": "visual",
    })
    tracked = execute(initialized, [parent_track]).bundle
    card = capture_clip_add(tracked, "parent", {
        "id": "card", "clipType": "am-type", "track": "picture", "at": 0,
        "hold": 1,
    })
    carded = execute(tracked, [card]).bundle
    if current_form:
        authored_card = copy.deepcopy(carded)
        authored_card["parent"]["clips"][0]["app"] = {"astrid": {"for": 0.5}}
        authored_card["parent"]["config"]["clips"] = copy.deepcopy(
            authored_card["parent"]["clips"])
        card_declaration = emit_document_commands(carded, authored_card)
        authored_card = execute(carded, card_declaration).bundle
    else:
        authored_card = copy.deepcopy(carded)
        authored_card["parent"]["clips"][0]["app"] = {
            "astrid": {"on": DECLARATION, "for": 0.5},
        }
        # Keep the two physical parent clip mirrors identical while constructing
        # the authored declaration; the emitter validates both before projection.
        authored_card["parent"]["config"]["clips"] = copy.deepcopy(
            authored_card["parent"]["clips"])
        card_declaration = emit_document_commands(carded, authored_card)
        authored_card = execute(carded, card_declaration).bundle
    occurrence = capture_occurrence_create(
        authored_card, "occ", "shot", name="Voice", track="picture", on=2,
        duration=duration_ms / 1000,
    )
    occurred = execute(authored_card, [occurrence]).bundle

    voices = [{"id": "voice", "clipType": "media", "track": "vo", "asset": "take",
               "at": 1, "from": 2, "to": 6, "speed": 2}]
    if current_form and duration_ms == 1750:
        # The boundary case's selected second word ends at the occurrence's
        # half-open boundary; keep the source interval itself valid for C0.
        voices[0]["to"] = 3.5
    if source_window is not None:
        voices[0]['source_window'] = source_window
    if reused:
        voices.append(dict(voices[0], id="other", at=4))
    after = copy.deepcopy(occurred)
    # timeline.init accepts only its supported bootstrap fps; retain the exact
    # authored rational output through the emitter's guarded before/after patch.
    after["parent"]["config"]["output"]["fps"] = copy.deepcopy(FPS)
    after["parent"]["config"]["theme_overrides"] = {"visual": {"canvas": {"fps": FPS}}}
    after["shots"]["shot"]["internal_timeline"]["output"]["fps"] = copy.deepcopy(FPS)
    after["shots"]["shot"]["payload"].update(
        {"items": [], **(opaque if opaque_pin_scope == "shot" else {})})
    internal = after["shots"]["shot"]["internal_timeline"]
    internal["tracks"] = [{"id": "vo", "kind": "audio"}]
    internal["clips"] = voices
    internal["registry"] = {"assets": {"take": {"media_id": media, "type": "audio"}}}
    if opaque_pin_scope == "parent":
        after["parent"]["opaque"] = copy.deepcopy(opaque)
    if opaque_pin_scope == "occurrence":
        after["placements"][0]["opaque"] = copy.deepcopy(opaque)
    changes = emit_document_commands(occurred, after)
    # occurrence.create quantizes its input placement.  The fixture's source
    # contract retains the original literal 2000ms placement, so carry that
    # authored field through the generic patch path with its exact guard.
    placement_start = occurred["placements"][0]["placement"]["start_ms"]
    changes[0]["args"]["changes"].insert(0, {
        "op": "set", "ref": "parent",
        "path": ["occurrences", "occurrence_id:occ", "placement", "start_ms"],
        "before": {"state": "present", "value": placement_start}, "value": 2000,
    })
    media_admission = {
        "schema_version": 1, "op": "media.admit", "version": "1", "select": None,
        "args": {"inputs": [{"pin": {"media_id": media, "content_digest": media}}]},
    }
    if current_form:
        # Allocate the real source track through its native command before the
        # source-bound document patch.  The bootstrap has no legacy declaration;
        # the following anchor is the authenticated current-form first use.
        source_track = capture_track_create(occurred, "occurrence:occ", {
            "id": "vo", "kind": "audio"})
        source_before = execute(occurred, [source_track]).bundle
        source_after = copy.deepcopy(source_before)
        source_after["shots"]["shot"]["internal_timeline"]["output"]["fps"] = 30
        source_after["shots"]["shot"]["payload"].update(
            {"items": [], **(opaque if opaque_pin_scope == "shot" else {})})
        internal = source_after["shots"]["shot"]["internal_timeline"]
        internal["clips"] = voices
        internal["registry"] = {"assets": {"take": {"media_id": media, "type": "audio"}}}
        if opaque_pin_scope == "parent":
            source_after["parent"]["opaque"] = copy.deepcopy(opaque)
        if opaque_pin_scope == "occurrence":
            source_after["placements"][0]["opaque"] = copy.deepcopy(opaque)
        source_changes = emit_document_commands(source_before, source_after)
        placement_start = source_before["placements"][0]["placement"]["start_ms"]
        source_changes[0]["args"]["changes"].insert(0, {
            "op": "set", "ref": "parent",
            "path": ["occurrences", "occurrence_id:occ", "placement", "start_ms"],
            "before": {"state": "present", "value": placement_start}, "value": 2000,
        })
        initial = submit([initialize, parent_track, card, occurrence, source_track,
                          *source_changes, media_admission], "initial-commit")
        anchored = bundle_from_document(initial)
        anchored["source_mapping"]["pinned_documents"] = [{
            "pin": copy.deepcopy(pin), "document": copy.deepcopy(source)}]
        choice = {
            "source": "occurrence:occ/clip:voice", "document": copy.deepcopy(pin),
            "word_ids": [content["segments"][0]["words"][1]["id"]],
        }
        anchor = capture_anchor(anchored, "parent/clip:card", "on", '"again"', choice)
        anchor["intent"] = {"anchor": "follow"}
        initial = submit([anchor], "initial-word-anchor")
    else:
        initial = submit([initialize, parent_track, card, *card_declaration, occurrence,
                          media_admission, *changes], "initial-commit")

    if current_form:
        # Exercise the supported first-use path through the registered provider:
        # authenticate the exact transcript choice, then clear the authored
        # choice while C0 retains the source pin for later reads.
        clear = capture_anchor(bundle_from_document(initial),
                               "parent/clip:card", "on", None, None)
        cleared = submit([clear], "retain-and-clear-word-anchor")
        corrected = bundle_from_document(cleared)
        corrected_after = copy.deepcopy(corrected)
        corrected_after["parent"]["config"]["output"]["fps"] = copy.deepcopy(FPS)
        corrected_after["parent"]["config"]["theme_overrides"] = {
            "visual": {"canvas": {"fps": FPS}}}
        corrected_after["shots"]["shot"]["internal_timeline"]["output"]["fps"] = copy.deepcopy(FPS)
        corrected_after["parent"]["config"]["clips"] = copy.deepcopy(
            corrected_after["parent"]["clips"])
        corrections = emit_document_commands(corrected, corrected_after)
        corrections.append({"schema_version": 1, "op": "doc.patch", "version": "1",
            "select": None, "args": {"changes": [{
                "op": "set", "ref": "parent", "path": ["config", "clips"],
                "before": {"state": "present",
                           "value": copy.deepcopy(corrected["parent"]["config"]["clips"])},
                "value": copy.deepcopy(corrected_after["parent"]["config"]["clips"]),
            }]}})
        corrections.append({"schema_version": 1, "op": "doc.patch", "version": "1",
            "select": None, "args": {"changes": [{
                "op": "set", "ref": "parent", "path": ["clips", "id:card", "app", "astrid", "for"],
                "before": {"state": "absent"}, "value": 0.5,
            }]}})
        corrections.append({"schema_version": 1, "op": "doc.patch", "version": "1",
            "select": None, "args": {"changes": [{
                "op": "remove", "ref": "parent",
                "path": ["clips", "id:card", "app", "astrid", "word_anchors"],
                "before": {"state": "present", "value": {}},
            }]}})
        submit(corrections, "current-baseline-corrections")

    service.close()
    return {'project': project, 'pin': pin, 'source': source}


def _retained_case(tmp_path, monkeypatch, request, *, current_form):
    runtime = use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import route_path

    root = tmp_path / 'realm'
    script = """
import json, sys
from tests.editing_conformance.native_word_anchor_fixture import seed
options = json.loads(sys.argv[2])
print(json.dumps(seed(sys.argv[1], options.pop('reused', False), **options)))
"""
    options = getattr(request, 'param', False)
    options = options if isinstance(options, dict) else {'reused': options}
    requested_options = dict(options)
    seed_options = dict(options)
    if current_form:
        if seed_options.get('duration_ms') == 100:
            seed_options.pop('duration_ms')
        if 'source_window' in seed_options:
            seed_options.pop('source_window')
    options = {**seed_options, 'current_form': current_form}
    seeded = subprocess.run([sys.executable, '-c', script, str(root),
        json.dumps(options)], text=True, capture_output=True, check=True,
        env={**os.environ,
             'PYTHONPATH': str(Path(__file__).resolve().parents[2]) + os.pathsep + str(runtime),
             'PYTHONDONTWRITEBYTECODE': '1'}, cwd=tmp_path)
    values = json.loads(seeded.stdout)
    project, pin, source = (values[key] for key in ('project', 'pin', 'source'))
    service = RuntimeService(root)
    case = SimpleNamespace(service=service, root=root, project=project, pin=pin, source=source,
                           hydrated=[], documents={}, negative_options=requested_options)

    class NoSceneImports:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(('composition', 'remotion', 'astrid.sdk.authoring_render_preview')) or '.scene' in fullname:
                raise AssertionError('commit imported scene evaluation: ' + fullname)

    def no_renderer(*args, **kwargs):
        raise AssertionError('commit evaluated a scene with the renderer off')

    import runtime_protocol.service as service_module
    import runtime_protocol.visual_seam as visual_seam

    monkeypatch.setattr(sys, 'meta_path', [NoSceneImports(), *sys.meta_path])
    monkeypatch.setattr(service_module, 'evaluate_closure', no_renderer)
    monkeypatch.setattr(visual_seam, 'evaluate_closure', no_renderer)

    def head():
        value = case.service.get_project_timeline(project, "film")
        return {"revision_id": value["head_revision_id"], "publication_id": value["head_publication_id"]}

    def request(commands, base=None):
        base = base or head()
        if (current_form and base['revision_id'] in case.documents
                and any(command.get('op') == 'clip.anchor' for command in commands)
                and ('duration_ms' in requested_options and 'duration_ms' not in seed_options
                     or 'source_window' in requested_options and 'source_window' not in seed_options)):
            current_bundle = agent.bundle_from_document(case.documents[base['revision_id']])
            changes = []
            if 'duration_ms' in requested_options and 'duration_ms' not in seed_options:
                current = current_bundle['placements'][0]['duration_ms']
                changes.append({'op': 'set', 'ref': 'parent',
                    'path': ['occurrences', 'occurrence_id:occ', 'duration_ms'],
                    'before': {'state': 'present', 'value': current},
                    'value': requested_options['duration_ms']})
            if 'source_window' in requested_options and 'source_window' not in seed_options:
                changes.append({'op': 'set', 'ref': 'internal:film',
                    'path': ['clips', 'id:voice', 'source_window'],
                    'before': {'state': 'absent'},
                    'value': copy.deepcopy(requested_options['source_window'])})
            commands = [{"schema_version": 1, "op": "doc.patch", "version": "1",
                         "select": None, "args": {"changes": changes}}, *commands]
        label = "Timeline conformance edit"
        return {"api_version": 1, "project_id": project, "timeline_id": "film",
                "base_revision_id": base["revision_id"], "base_publication_id": base["publication_id"],
                "commands": commands, "label": label, "steps": _steps(commands, label),
                "merge": _merge_mode(commands), "reads": [], "dry_run": False,
                "source": {"via": "python"}, "rationale": label,
                "group_id": "timeline-conformance"}

    def submit(body, key, authorize=lambda: None):
        result = case.service.commit_timeline(project, "film", body, idempotency_key=key,
            publisher="timing-editor", invocation_path=route_path(project, "film"), authorize=authorize)
        document = result["data"]["canonical_document"]
        case.documents[document["parent_revision"]["revision_id"]] = document
        return result

    service.register_timeline_commit_provider(agent.create_runtime_commit_service())
    hydrate = agent._hydrate

    def observed(port, bundle, calls=(), **kwargs):
        result = hydrate(port, bundle, calls, **kwargs)
        case.hydrated.append(copy.deepcopy(bundle))
        return result

    monkeypatch.setattr(agent, "_hydrate", observed)
    case.seed = submit(request([{"schema_version": 1, "op": "track.create", "version": "1",
        "select": "parent", "created": ["parent/track:initial"],
        "args": {"row": {"id": "initial", "kind": "audio"}}}]), "initial")

    def restart(provider=None):
        case.service.close()
        case.service = RuntimeService(root)
        case.service.register_timeline_commit_provider(provider or agent.create_runtime_commit_service())

    def create(track, base=None):
        base = base or head()
        bundle = agent.bundle_from_document(case.documents[base["revision_id"]])
        bundle["source_mapping"]["pinned_documents"] = copy.deepcopy(
            case.hydrated[-1]["source_mapping"].get("pinned_documents", []))
        command = capture_track_create(bundle, "parent", {"id": track, "kind": "audio"})
        return request([command], base)

    case.head, case.request, case.submit, case.restart, case.create = head, request, submit, restart, create
    try:
        yield case
    finally:
        case.service.close()


@pytest.fixture
def retained(tmp_path, monkeypatch, request):
    yield from _retained_case(tmp_path, monkeypatch, request, current_form=False)


@pytest.fixture
def current_retained(tmp_path, monkeypatch, request):
    yield from _retained_case(tmp_path, monkeypatch, request, current_form=True)
