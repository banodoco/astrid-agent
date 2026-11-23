"""Occurrence media swap capture and its retained word-timing boundary."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.media_swap import capture_document_swaps
from tests.editing_conformance.test_recipe_occurrence_media import inputs

OWNER = "occurrence:occ-a"
CLIP = OWNER + "/clip:clip-a"
OLD_ID = "sha256:" + "1" * 64
NEW_ID = "sha256:" + "2" * 64


class CatalogTransport:
    """Small public-catalog surface returning exact selection and verification rows."""

    def __init__(self, *, kind="image"):
        self.kind = kind
        self.variants = [
            {"variant_id": "new-variant", "object_id": NEW_ID},
        ]
        self.selections = {}
        self.unverified = False

    def list_generations(self, project, *, cursor=None, limit=50):
        return ([{"generation_id": "new-generation"}], None)

    def list_variants(self, generation_id, *, cursor=None, limit=50):
        variants = [row for row in self.variants if row["variant_id"] == "new-variant"]
        return (variants, None)

    def list_project_objects(self, project, *, cursor=None, limit=50, **filters):
        return ([{"object_id": NEW_ID, "content_digest": NEW_ID,
                  "media_type": self.kind + "/fixture", "size": 123,
                  **({"duration_seconds": 6} if self.kind == "video" else {})}], None)

    def get_project_media_selection(self, project, selectors):
        alias = selectors["asset"]
        generation = selectors.get("generation_id") or "new-generation"
        variant = selectors.get("variant_id") or "new-variant"
        object_id = NEW_ID
        selection = {"asset": alias, "generation_id": generation, "variant_id": variant,
                     "object_id": object_id, "catalog_digest": "sha256:" + "a" * 64}
        self.selections[alias] = selection
        return selection

    def get_project_verified_media(self, project, selection):
        value = dict(selection)
        if self.unverified:
            value["object_id"] = OLD_ID if selection["object_id"] == NEW_ID else NEW_ID
        return {"selection": value, "media_type": self.kind + "/fixture",
                "size": 123, **({"duration_seconds": 6} if self.kind == "video" else {})}


def _legacy_bundle(kind="image", *, intrinsic=False):
    document, _, _, _ = inputs(kind)
    bundle = bundle_from_document(document)
    owner = OWNER
    registry = State(bundle).registry(owner)["assets"]
    registry["picture"] = {
        "media_id": OLD_ID, "type": kind + ("/mp4" if kind == "video" else "/jpeg"),
        "source": "legacy-camera-roll/shot", "content_sha256": OLD_ID.removeprefix("sha256:"),
    }
    shot_id = State(bundle).occurrence(owner)["shot_id"]
    row = State(bundle).entity(CLIP)
    row["clipType"] = "am-footage"
    row["elementRef"] = {"packId": "astrid_motion", "kind": "effect", "id": "am-footage",
                         "revision": "sha256:" + "4" * 64}
    row["app"] = {"astrid": {"on": '"unbound legacy word"'}}
    row["hold"] = 2
    if intrinsic:
        row.pop("hold")
        row["from"], row["to"], row["speed"] = 0, 2, 1
    else:
        row.pop("from", None)
        row.pop("to", None)
        row.pop("speed", None)
    # Keep this fixture focused on the clip and registry mirrors. The recipe
    # fixture's independent shot item points at the same old object, so remove
    # that unrelated reference from both the bundle and its source document.
    payload = bundle["shots"][shot_id]["payload"]
    payload["items"] = []
    document = bundle["source_mapping"]["_runtime_document"]
    document["shot_revisions"][0]["payload"]["items"] = []
    document["shot_revisions"][0]["content_digest"] = _digest(document["shot_revisions"][0]["payload"])
    document["dependency_manifest"] = _manifest(
        document["shot_revisions"], document["internal_timeline_revisions"],
        document["dependency_manifest"]["media"], document["dependency_manifest"]["documents"],
    )
    return bundle


def _candidate(base, kind):
    candidate = copy.deepcopy(base)
    State(candidate).entity(CLIP)["asset"] = "new-footage"
    State(candidate).registry(OWNER)["assets"]["new-footage"] = {
        "media_id": NEW_ID, "type": kind + ("/mp4" if kind == "video" else "/jpeg"),
        "generationId": "new-generation", "variantId": "new-variant",
        **({"duration": 6} if kind == "video" else {}),
    }
    return candidate


@pytest.mark.parametrize("kind", ["image", "video"])
def test_capture_occurrence_swap_preserves_authored_footage_and_legacy_entry(kind):
    base = _legacy_bundle(kind)
    candidate = _candidate(base, kind)
    before = copy.deepcopy(base)
    old_entry = copy.deepcopy(State(base).registry(OWNER)["assets"]["picture"])
    target_before = copy.deepcopy(State(base).entity(CLIP))
    sibling_before = copy.deepcopy(State(base).entity("occurrence:occ-b/clip:clip-a"))
    parent_before = copy.deepcopy(base["parent"])
    placements_before = copy.deepcopy(base["placements"])

    commands, working, verified = capture_document_swaps(base, candidate, CatalogTransport(kind=kind))
    from astrid.sdk.timeline._commit_timing_validation import validate_candidate_timing

    validate_candidate_timing(working, baseline=base)

    assert {command["op"] for command in commands} == {"doc.patch", "media.admit"}
    changes = [change for command in commands if command['op'] == 'doc.patch'
               for change in command['args']['changes']]
    assert sorted(change['path'] for change in changes) == [
        ['clips', 'id:clip-a', 'asset'], ['registry', 'assets', 'new-footage']]
    assert len({change['ref'] for change in changes}) == 1
    target_after = State(working).entity(CLIP)
    assert target_after["asset"] == "new-footage"
    assert {key: value for key, value in target_after.items() if key != "asset"} == {
        key: value for key, value in target_before.items() if key != "asset"
    }
    assert target_after["elementRef"] == target_before["elementRef"]
    assert State(working).entity("occurrence:occ-b/clip:clip-a") == sibling_before
    assert working["parent"] == parent_before
    assert working["placements"] == placements_before
    assert State(working).registry(OWNER)["assets"]["picture"] == old_entry
    assert State(working).registry(OWNER)["assets"]["new-footage"]["media_id"] == NEW_ID
    assert target_after["hold"] == 2
    assert "from" not in target_after and "to" not in target_after and "speed" not in target_after
    assert verified
    assert base == before


@pytest.mark.parametrize("failure", ["unverified", "wrong-digest", "unverified-raw-object"])
def test_capture_refuses_unverified_or_ambiguous_legacy_media_without_mutation(failure):
    base = _legacy_bundle()
    candidate = _candidate(base, "image")
    transport = CatalogTransport()
    if failure == "unverified":
        transport.unverified = True
    elif failure == "wrong-digest":
        State(candidate).registry(OWNER)["assets"]["new-footage"]["content_sha256"] = "f" * 64
    elif failure == "unverified-raw-object":
        entry = State(candidate).registry(OWNER)["assets"]["new-footage"]
        entry.pop("generationId")
        entry.pop("variantId")
        transport.list_project_objects = lambda project, **kwargs: ([{
            "object_id": NEW_ID, "content_digest": OLD_ID,
            "media_type": "image/fixture", "size": 123}], None)
    before = copy.deepcopy((base, candidate))
    with pytest.raises(ValueError):
        capture_document_swaps(base, candidate, transport)
    assert (base, candidate) == before


def test_legacy_word_binding_exemption_ignores_asset_only_change_for_explicit_visual_hold():
    from astrid.sdk.timeline._commit_timing_validation import _legacy_exemptions

    baseline = _legacy_bundle("image")
    candidate = _candidate(baseline, "image")
    shot_id = State(baseline).occurrence(OWNER)["shot_id"]
    key = ("shot", shot_id, "clip-a")
    assert (key, "on") in _legacy_exemptions(candidate, baseline)

    for field, value in [("on", '"different word"'), ("at", 1.5), ("hold", 3), ("speed", 2)]:
        changed = copy.deepcopy(candidate)
        row = State(changed).entity(CLIP)
        if field == "on":
            row["app"]["astrid"][field] = value
        else:
            row[field] = value
        assert (key, "on") not in _legacy_exemptions(changed, baseline)


def test_legacy_word_binding_asset_change_is_not_exempt_for_intrinsic_duration():
    from astrid.sdk.timeline._commit_timing_validation import _legacy_exemptions

    baseline = _legacy_bundle("video", intrinsic=True)
    candidate = _candidate(baseline, "video")
    shot_id = State(baseline).occurrence(OWNER)["shot_id"]
    key = ("shot", shot_id, "clip-a")
    assert (key, "on") in _legacy_exemptions(baseline, baseline)
    assert (key, "on") not in _legacy_exemptions(candidate, baseline)
    from astrid.sdk.timeline._commit_timing_validation import validate_candidate_timing
    from astrid.sdk.timeline.compiler import TimingCompileError

    with pytest.raises(TimingCompileError, match="missing exact word binding"):
        validate_candidate_timing(candidate, baseline=baseline)


def test_media_typed_audio_keeps_its_asset_in_the_word_dependency_basis():
    from astrid.sdk.timeline._commit_timing_validation import _legacy_exemptions

    baseline = _legacy_bundle('image')
    State(baseline).entity(CLIP)['clipType'] = 'media'
    State(baseline).registry(OWNER)['assets']['picture']['type'] = 'audio/wav'
    candidate = _candidate(baseline, 'image')
    State(candidate).registry(OWNER)['assets']['new-footage']['type'] = 'audio/wav'
    shot_id = State(baseline).occurrence(OWNER)['shot_id']
    assert (("shot", shot_id, 'clip-a'), 'on') not in _legacy_exemptions(candidate, baseline)


def test_still_hold_clip_type_remains_authored_during_media_admission():
    base = _legacy_bundle("image")
    State(base).entity(CLIP)["clipType"] = "hold"
    candidate = _candidate(base, "image")
    commands, working, _ = capture_document_swaps(base, candidate, CatalogTransport())

    assert "media.admit" in {command["op"] for command in commands}
    assert State(working).entity(CLIP)["clipType"] == "hold"
    assert State(working).entity(CLIP)["asset"] == "new-footage"


@pytest.mark.parametrize('element', ['am-footage', 'pack-footage', 'hold'])
def test_exact_selected_native_swap_retains_legacy_registry_and_element_fields(element):
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_swap_media

    document, old, new, verified = inputs('image')
    base = bundle_from_document(document)
    row = State(base).entity(CLIP)
    row.update(clipType=element, params={'zoom': 2.4}, app={'astrid': {
        'on': '"unbound legacy word"', 'sequence': {'id': 'seq', 'index': 0}}})
    if element == 'pack-footage':
        row['elementRef'] = {'packId': 'fixture', 'kind': 'effect', 'id': element, 'revision': 'fixture'}
    entry = State(base).registry(OWNER)['assets'][old['asset']]
    entry.update(type='image/png', content_sha256=old['object_id'], source='old-camera.png')
    before = copy.deepcopy(base)
    call = capture_occurrence_swap_media(base, CLIP, old, new, verified_media=verified)
    plan = execute(base, [call], verified_media=verified)
    expected = copy.deepcopy(row)
    expected['asset'] = new['asset']
    assert State(plan.bundle).entity(CLIP) == expected
    assert State(plan.bundle).registry(OWNER)['assets'][old['asset']] == entry
    assert base == before


def test_project_object_without_catalog_variant_is_admitted_by_exact_raw_pin():
    base = _legacy_bundle("image")
    candidate = _candidate(base, "image")
    entry = State(candidate).registry(OWNER)["assets"]["new-footage"]
    entry.pop("generationId")
    entry.pop("variantId")
    transport = CatalogTransport()
    commands, working, _ = capture_document_swaps(base, candidate, transport)

    admission, = [command for command in commands if command["op"] == "media.admit"]
    inputs = admission["args"]["inputs"]
    assert len(inputs) == 1 and "selection" not in inputs[0]
    assert inputs[0]["pin"] == {"media_id": NEW_ID, "content_digest": NEW_ID}
    assert State(working).entity(CLIP)["asset"] == "new-footage"


def test_provider_accepts_captured_legacy_footage_swap_with_unbound_word_anchor():
    from astrid.sdk.timeline.commit import create_runtime_commit_service
    from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm
    from tests.sdk.test_timeline_commit_provider import DetachedPort

    kind = "image"
    base = _legacy_bundle(kind)
    candidate = _candidate(base, kind)
    transport = CatalogTransport(kind=kind)
    commands, _, verified = capture_document_swaps(base, candidate, transport)
    realm = MediaRealm(document_from_bundle(base), verified)
    context = realm.context(commands)
    steps = []
    for ordinal, command in enumerate(commands):
        steps.append({
            "id": f"swap-{ordinal}", "label": "Swap clip media", "intent": "Replace selected footage",
            "tags": ["media-swap"], "command_ordinals": [ordinal],
            "patch": command["args"]["changes"] if command["op"] == "doc.patch" else [], "reads": [],
        })
    context["request"].update(label="Swap clip media", merge="atomic", reads=[], steps=steps,
                              source={"via": "python"}, rationale="Replace selected footage",
                              group_id="occurrence-media-swap")
    context["invocation"]["body"] = copy.deepcopy(context["request"])
    context["request_hash"] = _digest(context["invocation"])
    # Match TimelineSession's default semantic boundaries: admission observes
    # the resulting closure but never shares a document patch assertion.
    context['request']['steps'] = [
        {'id': f'step-{index}', 'label': 'Swap clip media', 'intent': '', 'tags': [],
         'command_ordinals': [index],
         'patch': copy.deepcopy(command['args']['changes']) if command['op'] == 'doc.patch' else []}
        for index, command in enumerate(commands)]

    class AdmissionPort(DetachedPort):
        def raw_media_info(self, pin):
            return {"object_id": pin["media_id"], "content_digest": pin["content_digest"],
                    "media_type": "image/fixture", "size": 123}

        def selected_media(self, selection):
            matches = [row for row in self.realm.verified if row.get("selection") == selection]
            if len(matches) != 1:
                raise ValueError("selection is absent from fake Runtime catalog")
            return copy.deepcopy(matches[0])

    result = create_runtime_commit_service().commit(context, AdmissionPort(realm, context))

    output = result["data"]["canonical_document"]
    output_bundle = bundle_from_document(output)
    assert State(output_bundle).entity(CLIP)["asset"] == "new-footage"
    assert State(output_bundle).registry(OWNER)["assets"]["picture"] == State(base).registry(OWNER)["assets"]["picture"]
    assert State(output_bundle).entity(CLIP)["app"] == State(base).entity(CLIP)["app"]


def test_sdk_capture_free_edits_reuses_shared_swap_recipe_without_source_mapping_noise():
    from astrid.sdk.timeline.commit_session import Timeline
    from astrid.sdk.timeline.model import Clip

    base = _legacy_bundle("image")
    transport = CatalogTransport(kind="image")
    session = SimpleNamespace(
        client=SimpleNamespace(_remote=SimpleNamespace(_transport=transport)),
        project_id="project-1",
        identity={"realm_id": "realm-1"},
    )
    timeline = Timeline(session, base, base.get("base_parent"), evaluating=True)
    shot_id = State(timeline.bundle).occurrence(OWNER)["shot_id"]
    Clip(timeline, shot_id, State(timeline.bundle).entity(CLIP)).swap_asset({
        "key": "new-footage", "media_id": NEW_ID, "type": "image/jpeg",
        "generationId": "new-generation", "variantId": "new-variant",
    })

    timeline._capture_free_edits()

    assert {command["op"] for command in timeline._commands} == {"doc.patch", "media.admit"}
    document_changes = [change for command in timeline._commands if command["op"] == "doc.patch"
                        for change in command["args"]["changes"]]
    assert sorted(change["path"] for change in document_changes) == [
        ["clips", "id:clip-a", "asset"], ["registry", "assets", "new-footage"]]
    assert [step["command_ordinals"] for step in timeline._steps] == [[0], [1]]
    assert timeline._steps[0]["patch"] == document_changes
    assert timeline._steps[1]["patch"] == []
    assert any(row.get("pin", {}).get("media_id") == NEW_ID for row in timeline._verified_media)
    assert State(timeline.bundle).entity(CLIP)["asset"] == "new-footage"
    assert State(timeline.bundle).registry(OWNER)["assets"]["picture"] == \
        State(base).registry(OWNER)["assets"]["picture"]

    before_preview = copy.deepcopy(timeline.bundle)
    result = {"data": {"canonical_document": document_from_bundle(
        timeline._execution_bundle(timeline.bundle)), "commands": copy.deepcopy(timeline._commands)}}
    timeline._add_preview_diff(result, timeline.bundle)
    assert result["data"]["diff"]["changes"]
    assert timeline.bundle == before_preview


def _parsed_swap(**overrides):
    defaults = {
        "project": "project-1", "ref": "film", "clip": "clip-a", "cut": None, "near": None,
        "swap_asset": "new-footage", "dry_run": False, "idempotency_key": "swap-key",
        "json": False, "no_sweep": False, "file": None, "draft": False,
        "trim_in": None, "trim_out": None, "on": None, "add_layer": None,
    }
    values = {**defaults, **overrides}
    values["native_trim_defaults"] = {
        "project": None, "ref": None, "clip": None, "cut": None, "near": None,
        "swap_asset": None, "dry_run": False, "idempotency_key": None,
        "json": False, "no_sweep": False, "trim_in": None, "trim_out": None,
        "on": None, "add_layer": None,
    }
    return SimpleNamespace(client=object(), **values)


def test_cli_routes_only_an_ordinary_selected_asset_swap(monkeypatch):
    from astrid.sdk.timeline import store
    from astrid.packs.timeline import cli_native

    monkeypatch.setattr(store, "find_draft", lambda *args, **kwargs: None)
    parsed = _parsed_swap()
    assert cli_native.ordinary_swap_requested(parsed)

    routed = []
    monkeypatch.setattr(cli_native, "commit_edit", lambda value, *, edit=None: routed.append(edit) or "handled")
    assert cli_native.dispatch_ordinary_edit(parsed) == "handled"
    assert routed == [cli_native.commit_swap]

    routed.clear()
    assert cli_native.dispatch_ordinary_edit(_parsed_swap(trim_in=1)) is NotImplemented
    assert routed == []
    assert not cli_native.ordinary_swap_requested(_parsed_swap(draft=True))
    monkeypatch.setattr(store, "find_draft", lambda *args, **kwargs: Path("/tmp/film.pending.json"))
    assert not cli_native.ordinary_swap_requested(_parsed_swap())


def test_cli_swap_adapter_reuses_session_outbox_and_calls_sdk_swap_asset(monkeypatch):
    from astrid.packs.timeline import cli_native
    from astrid.sdk.timeline import commit_session

    calls = []

    class Clip:
        def swap_asset(self, asset):
            calls.append(("swap_asset", asset))

    class Timeline:
        def clip(self, query, *, near=None, cut=None):
            calls.append(("clip", query, near, cut))
            return Clip()

        def commit(self, function, **kwargs):
            function(self)
            calls.append(("commit", kwargs))
            return "committed"

    timeline = Timeline()
    opened = []
    monkeypatch.setattr(commit_session.Timeline, "open", lambda *args, **kwargs: opened.append((args, kwargs)) or timeline)
    session = SimpleNamespace(project_id="project-1", timeline_id="film", client=object(),
                              outbox=SimpleNamespace(path=Path("/tmp/shared-outbox.json")))
    parsed = _parsed_swap(near=12.5, cut="cut-a", dry_run=True)

    result = cli_native.commit_swap(session, parsed, {
        "dry_run": True, "idempotency_key": parsed.idempotency_key, "via": "cli",
    })

    assert result == "committed"
    assert opened == [(("project-1", "film"), {
        "client": session.client, "outbox_path": session.outbox.path,
    })]
    assert calls == [
        ("clip", "clip-a", 12.5, "cut-a"),
        ("swap_asset", "new-footage"),
        ("commit", {"label": "Swap clip media", "dry_run": True, "idempotency_key": "swap-key"}),
    ]


def _retained_anchor_swap():
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.native_word_anchor import capture_anchor
    from tests.editing_conformance.test_native_word_anchor_vectors import fixture

    bundle, pin, content = fixture()
    verified = copy.deepcopy(bundle['source_mapping']['pinned_documents'][0])
    choice = {'source': 'occurrence:occ-a/clip:voice', 'document': pin,
              'word_ids': [content['segments'][0]['words'][0]['id']]}
    anchored = execute(bundle, [capture_anchor(bundle, 'parent/clip:clip-a', 'on', 'again', choice)]).bundle
    document = document_from_bundle(anchored)
    internal = document['internal_timeline_revisions'][0]
    internal['payload']['clips'].append({'id': 'visual', 'clipType': 'media', 'track': 'vo',
                                         'asset': 'old-visual', 'at': 0, 'hold': 2})
    internal['payload']['registry']['assets']['old-visual'] = {
        'media_id': OLD_ID, 'type': 'image/jpeg', 'source': 'old.jpg'}
    internal['content_digest'] = _digest(internal['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'],
        document['internal_timeline_revisions'], document['dependency_manifest']['media'], [pin])
    return bundle_from_document(document), verified


@pytest.mark.parametrize('damage', [None, 'mismatch', 'revoked'])
def test_sdk_swap_capture_hydrates_only_exact_retained_parent_anchor(damage):
    from astrid.sdk.timeline.commit_session import Timeline
    from astrid.sdk.timeline.model import Clip

    base, verified = _retained_anchor_swap()
    transport = CatalogTransport()
    class Missing(Exception):
        code = 'not_found'
    def get_document(project, pin):
        if damage == 'revoked':
            raise Missing()
        result = copy.deepcopy(verified)
        if damage == 'mismatch':
            result['document']['version'] += 1
        return result
    transport.get_project_verified_document = get_document
    session = SimpleNamespace(client=SimpleNamespace(_remote=SimpleNamespace(_transport=transport)),
                              project_id=base['project_id'], identity={'realm_id': 'realm-1'})
    timeline = Timeline(session, base, base.get('base_parent'), evaluating=True)
    shot_id = State(timeline.bundle).occurrence(OWNER)['shot_id']
    Clip(timeline, shot_id, State(timeline.bundle).entity(OWNER + '/clip:visual')).swap_asset({
        'key': 'new-footage', 'media_id': NEW_ID, 'type': 'image/jpeg',
        'generationId': 'new-generation', 'variantId': 'new-variant'})
    before = copy.deepcopy(timeline.bundle['parent'])
    if damage:
        message = {'mismatch': 'scope or version', 'revoked': 'missing or unavailable'}[damage]
        with pytest.raises(ValueError, match=message):
            timeline._capture_free_edits()
        assert timeline.bundle['parent'] == before
    else:
        timeline._capture_free_edits()
        assert {command['op'] for command in timeline._commands} == {'doc.patch', 'media.admit'}
        assert timeline.bundle['parent'] == before
        assert State(timeline.bundle).entity(OWNER + '/clip:visual')['asset'] == 'new-footage'
        assert 'pinned_documents' not in timeline.bundle['source_mapping']


@pytest.mark.parametrize("route", ["free_edit", "command", "track_rename"])
@pytest.mark.parametrize("damage", [None, "mismatch", "revoked"])
def test_public_edit_authenticates_retained_parent_anchor(monkeypatch, route, damage):
    from contextlib import nullcontext

    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.commit_session import TimelineSession

    bundle, verified = _retained_anchor_swap()
    exact_pin = verified["pin"]
    project = bundle["project_id"]
    reads = []
    sent = []

    class Missing(Exception):
        code = "not_found"

    class Transport:
        def get_project_verified_document(self, project_id, pin):
            reads.append((project_id, copy.deepcopy(pin)))
            if damage == "revoked":
                raise Missing()
            result = copy.deepcopy(verified)
            if damage == "mismatch":
                result["document"]["version"] += 1
            return result

        def __getattr__(self, name):
            raise AssertionError(f"unexpected transport call {name}")

    def submit(commands, **kwargs):
        sent.append((copy.deepcopy(commands), kwargs["dry_run"]))
        return {"data": {"status": "preview"}, "receipt": None}

    client = SimpleNamespace(_remote=SimpleNamespace(_transport=Transport()))
    base = {"revision_id": "retained-head", "publication_id": "accepted-publication"}
    durable = {"base": base, "pending": [], "flight": None}
    session = SimpleNamespace(
        project_id=project, timeline_id=bundle["timeline_id"], client=client,
        identity={"realm_id": "realm-1"}, outbox=SimpleNamespace(locked=nullcontext),
        _state=lambda: durable, submit=submit,
    )
    monkeypatch.setattr(TimelineSession, "open", lambda *a, **k: session)
    monkeypatch.setattr("astrid.sdk.timeline.store.fetch_bundle", lambda *a, **k: copy.deepcopy(bundle))

    timeline = Timeline.open(project, bundle["timeline_id"], client=client)
    title = "Renamed with retained word timing"
    if route == "free_edit":
        timeline.bundle["parent"]["name"] = title

    def run():
        if route == "command":
            timeline.command({
                "schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
                "args": {"changes": [{
                    "op": "set", "ref": "parent", "path": ["name"], "value": title,
                    "before": {"state": "present", "value": bundle["parent"]["name"]},
                }]},
            })
        elif route == "track_rename":
            timeline.track("picture").rename(title)
        return timeline.commit("Rename film", dry_run=True)

    before = copy.deepcopy(timeline.bundle)
    anchor = copy.deepcopy(bundle["parent"]["clips"][0])
    if damage:
        message = {"mismatch": "scope or version", "revoked": "missing or unavailable"}[damage]
        with pytest.raises(TimelineEditError, match=message):
            run()
        assert timeline.bundle == before
        assert timeline._commands == timeline._steps == timeline._verified_media == sent == []
        assert durable == {"base": base, "pending": [], "flight": None}
    else:
        assert run()["data"]["status"] == "preview"
        expected_op = "track.set" if route == "track_rename" else "doc.patch"
        assert [command["op"] for command in sent[0][0]] == [expected_op]
        assert sent[0][1] is True
        if route == "track_rename":
            assert State(timeline.bundle).entity("parent/track:picture")["label"] == title
        else:
            assert timeline.bundle["parent"]["name"] == title
        assert timeline.bundle["parent"]["clips"][0] == anchor
    assert reads == [(project, exact_pin)]
    assert "pinned_documents" not in timeline.bundle["source_mapping"]
    assert "_runtime_document" not in timeline.bundle["source_mapping"]
