"""Every commit is checked with the readers' eyes.

Render and visualize read a timeline through one shared projection and accept
only ``clip.asset`` media selectors. These tests commit through each write
command on a fixture that the readers can open, then validate the visualize
projection of the result with the readers' strict schema; they also check
that existing ``media_id`` clips become viewable through the shared
translation, and that the commit gate refuses a result no reader can open.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate, open_authoring_bundle
from astrid.core.timeline.reader_projection import (
    ReaderProjectionError,
    check_reader_projection,
    translate_media_selectors,
)
from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition
from astrid.sdk.timeline.commit import create_runtime_commit_service, reader_problem
from tests.packs.timeline.test_cli_commit_move import command  # noqa: F401 - pytest fixture
from tests.sdk.test_timeline_sync import connected as sync_connection  # noqa: F401 - pytest fixture

PICTURE = "sha256:" + "a" * 64
TAKE = "sha256:" + "b" * 64


def _digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def readable_payload():
    return {
        "config": {"output": {"fps": 30, "resolution": "1920x1080", "file": "out.mp4"},
                   "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}]},
        "registry": {"assets": {"pic": {"media_id": PICTURE, "content_sha256": "a" * 64,
                                        "type": "video", "duration": 10}}},
        "clips": [{"id": name, "track": "picture", "clipType": "media", "asset": "pic",
                   "at": index * 4, "hold": 4, "from": 0, "to": 4} for index, name in enumerate(("a", "b"))],
        "occurrences": [],
    }


def readable_bundle(payload=None):
    payload = payload or readable_payload()
    return open_authoring_bundle({"project_id": "p", "timeline_id": "t", "revision_id": "r0",
                                  "content_digest": _digest(payload), "payload": payload},
                                 shot_revisions=[], internal_timeline_revisions=[])


def visualize_projection(document):
    """The projection visualize sends to capture (Runtime's own capture projector)."""
    projected = create_runtime_commit_service().project_capture(
        copy.deepcopy(document), normalize_appearance=False)
    return projected["config"], projected["registry"]


def _accepted_documents(generated):
    return [response["data"]["canonical_document"] for _raw, response in generated.accepted.values()]


@pytest.fixture
def readable(command):  # noqa: F811
    run, generated = command
    bundle = readable_bundle()
    bundle["base_parent"]["publication_id"] = "pub0"
    generated.bundles["r0"] = bundle
    return run, generated


def _document_of(payload, revision="r1"):
    publication = compile_authoring_candidate(readable_bundle(payload)).publication
    return {"schema_version": 1, "project_id": "p", "timeline_id": "t",
            "parent_revision": {"revision_id": revision, "content_digest": publication["content_digest"],
                                "payload": publication["parent_composition"]},
            "shot_revisions": publication["shot_revisions"],
            "internal_timeline_revisions": publication["internal_timeline_revisions"],
            "dependency_manifest": {**publication["dependency_manifest"], "documents": []}}


def test_fixture_is_readable_before_any_commit():
    check_reader_projection(*visualize_projection(_document_of(readable_payload())))


@pytest.mark.parametrize("args", [
    ("edit", "t", "--clip", "a", "--on", "9"),
    ("edit", "t", "--clip", "a", "--trim-out", "2"),
    ("edit", "t", "--clip", "b", "--trim-in", "1"),
], ids=["move", "trim-out", "trim-in"])
def test_cli_write_command_round_trip_is_readable(readable, capsys, args):
    run, generated = readable
    assert run(*args, "--json") == 0, capsys.readouterr()
    capsys.readouterr()
    documents = _accepted_documents(generated)
    assert len(documents) == 1
    check_reader_projection(*visualize_projection(documents[0]))


def test_cli_undo_and_redo_round_trips_are_readable(readable, capsys):
    run, generated = readable
    assert run("edit", "t", "--clip", "a", "--on", "9", "--json") == 0
    assert run("undo", "t", "--commit", "pub1", "--json") == 0
    assert run("redo", "t", "--commit", "pub2", "--json") == 0
    capsys.readouterr()
    documents = _accepted_documents(generated)
    assert len(documents) == 3
    for document in documents:
        check_reader_projection(*visualize_projection(document))


def test_sdk_restore_round_trip_is_readable(sync_connection):  # noqa: F811
    from tests.sdk.test_timeline_sync import open_session

    client, generated, path = sync_connection
    bundle = readable_bundle()
    generated.bundles["r0"] = bundle
    session = open_session(sync_connection)
    session.move("parent/clip:a", 9)
    session.restore("r0", bundle["base_parent"]["content_digest"])
    documents = _accepted_documents(generated)
    assert len(documents) == 2
    for document in documents:
        check_reader_projection(*visualize_projection(document))


# --- a readable shot closure ---------------------------------------------

def _closure(clips, assets):
    """One shot placed once on a parent that render and visualize can open."""
    internal_payload = {
        "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}],
        "clips": copy.deepcopy(clips),
        "registry": {"assets": copy.deepcopy(assets)},
    }
    internal = {"project_id": "project-1", "timeline_id": "main", "revision_id": "internal-1",
                "content_digest": _digest(internal_payload), "payload": internal_payload}
    shot_payload = {"name": "Shot", "internal_timeline_revision_id": "internal-1"}
    shot = {"project_id": "project-1", "shot_id": "shot-1", "revision_id": "shot-rev-1",
            "internal_timeline_revision_id": "internal-1",
            "content_digest": _digest(shot_payload), "payload": shot_payload}
    parent_payload = {
        "config": {"output": {"fps": 30, "resolution": "1920x1080", "file": "out.mp4"},
                   "tracks": [{"id": "film", "kind": "visual", "label": "Film"}]},
        "clips": [],
        "occurrences": [{
            "occurrence_id": "occ-1", "shot_id": "shot-1", "shot_revision_id": "shot-rev-1",
            "placement": {"start_ms": 0, "track": "film"},
            "source_offset": {"start": 0, "end": 6000}, "duration_ms": 6000,
            "speed": {"numerator": 1, "denominator": 1}, "track": "film",
        }],
    }
    parent = {"project_id": "project-1", "timeline_id": "main", "revision_id": "parent-1",
              "content_digest": _digest(parent_payload), "payload": parent_payload}
    return parent, [shot], [internal]


def _clip(clip_id, at, **selector):
    return {"id": clip_id, "track": "picture", "clipType": "media", "at": at, "hold": 2, **selector}


# --- replace-parent-media -------------------------------------------------

def _replace_parent_media(*, register_new):
    """Run replace-parent-media on a readable shot; return the committed document."""
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
    from tests.timeline.test_authoring_bundle import _runtime_document

    assets = {"still": {"media_id": PICTURE, "type": "image"}}
    if register_new:
        assets["new-picture"] = {"media_id": TAKE, "type": "video"}
    parent, shots, internals = _closure([_clip("picture-1", 0, asset="still")], assets)
    source_document = _runtime_document(parent, shots, internals)
    base_bundle = bundle_from_document(source_document)
    base_bundle["source_mapping"]["_runtime_document"] = copy.deepcopy(source_document)

    class Transport:
        def list_timelines(self, project, *, cursor=None, limit=50):
            return ([{"project_id": project, "timeline_id": "main", "slug": "main"}], None)

        def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
            return {**copy.deepcopy(parent), "dependency_manifest": copy.deepcopy(
                source_document["dependency_manifest"])}

        def get_project_shot_revision(self, project_id, shot_id, revision):
            return copy.deepcopy(shots[0])

        def get_project_timeline_revision(self, project_id, timeline_id, revision):
            return copy.deepcopy(internals[0])

        def list_timeline_commits(self, project_id, timeline_id, *, cursor=None, limit=100):
            return {"data": {"items": [{
                "publication_id": "publication-1", "revision_id": "parent-1",
                "receipt": {"receipt_id": "publication-1", "result": {
                    "canonical_document": copy.deepcopy(source_document)}},
            }], "next_cursor": None}}

        def commit_timeline(self, project_id, timeline_id, body, *, idempotency_key):
            # Runtime verifies each media input before execution.
            verified = [{"object_id": row["pin"]["media_id"], "pin": copy.deepcopy(row["pin"])}
                        for call in body["commands"] if call["op"] == "media.admit"
                        for row in call["args"]["inputs"]]
            landed = execute(base_bundle, body["commands"], commit_request=body,
                             verified_media=verified).bundle
            canonical = document_from_bundle(landed)
            data = {"status": "accepted", "accepted_revision_id": "parent-2",
                    "previous_revision_id": "parent-1", "previous_publication_id": "publication-1",
                    "base_revision_id": "parent-1", "base_publication_id": "publication-1",
                    "rebased_onto": None, "canonical_document": canonical}
            receipt = {"receipt_id": "publication-2", "command_kind": "timeline.commit",
                       "idempotency_key": idempotency_key, "request_hash": "a" * 64,
                       "project_id": "project-1", "project_seq": [1, 1], "event_ids": ["event-1"],
                       "result": data, "created_at": "now"}
            return {"data": data, "receipt": receipt}

    base_problem = reader_problem(create_runtime_commit_service(), source_document)
    result = RemoteTimelines(Transport()).replace_parent_media(
        "project-1", "main", occurrence_id="occ-1", clip_id="picture-1",
        source_object_id=TAKE, expected_head="parent-1", idempotency_key="parent-replace-1",
    )
    assert result.ok, result.error
    return result.data["publication"]["publication"]["canonical_document"], base_problem


@pytest.mark.parametrize("register_new", [False, True], ids=["unregistered-media", "registered-asset"])
def test_replace_parent_media_round_trip_is_readable(register_new):
    document, base_problem = _replace_parent_media(register_new=register_new)
    assert base_problem is None, base_problem  # the fixture is readable before the edit
    child = document["internal_timeline_revisions"][0]["payload"]
    selected = next(clip for clip in child["clips"] if clip["id"] == "picture-1")
    # The clip selects a registry asset, never a bare media id.
    assert "media_id" not in selected and "object_id" not in selected
    assert child["registry"]["assets"][selected["asset"]]["media_id"] == TAKE
    if register_new:
        assert selected["asset"] == "new-picture"
    check_reader_projection(*visualize_projection(document))
    assert reader_problem(create_runtime_commit_service(), document) is None


# --- existing media_id clips through the shared translation ---------------

def _media_id_closure():
    """A shot whose clips select media by bare media_id, as cuts 7 and 9 of the live timeline do."""
    return _closure([_clip("picture-1", 0, asset="still"), _clip("picture-2", 2, media_id=TAKE),
                     _clip("picture-3", 4, media_id=PICTURE)],
                    {"still": {"media_id": PICTURE, "type": "image"}})


def test_existing_media_id_clips_are_viewable_through_the_shared_projection():
    parent, shots, internals = _media_id_closure()
    projected = project_runtime_parent_composition(parent, shot_revisions=shots,
                                                   internal_timeline_revisions=internals)
    clips = {clip["id"].rsplit(":", 1)[-1]: clip for clip in projected.config["clips"]}
    assert set(clips) == {"picture-1", "picture-2", "picture-3"}
    assert all("media_id" not in clip for clip in projected.config["clips"])
    assets = projected.registry["assets"]
    # A media_id that matches a registered asset reuses that alias ...
    assert assets[clips["picture-3"]["asset"]]["media_id"] == PICTURE
    assert clips["picture-3"]["asset"] == clips["picture-1"]["asset"]
    # ... and an unregistered one gets a registry entry of its own.
    assert assets[clips["picture-2"]["asset"]]["media_id"] == TAKE
    check_reader_projection(projected.config, projected.registry)


def test_existing_media_id_timeline_is_viewable_through_the_capture_projection():
    from tests.timeline.test_authoring_bundle import _runtime_document

    document = _runtime_document(*_media_id_closure())
    check_reader_projection(*visualize_projection(document))


def test_render_and_visualize_share_one_media_translation():
    from astrid.packs.rendering.actions.render.managed_timeline import _render_compatible_projection

    parent, shots, internals = _media_id_closure()
    projected = project_runtime_parent_composition(parent, shot_revisions=shots,
                                                   internal_timeline_revisions=internals)
    render_config, _render_registry = _render_compatible_projection(projected.config, projected.registry)
    assert [clip["asset"] for clip in render_config["clips"]] == [
        clip["asset"] for clip in projected.config["clips"]]


def test_translation_covers_every_authoring_spelling():
    config = {"clips": [
        {"id": "a", "asset_id": "pic"},
        {"id": "b", "object_id": PICTURE},
        {"id": "c", "media_id": TAKE.removeprefix("sha256:")},
    ]}
    registry = {"assets": {"pic": {"media_id": PICTURE}}}
    translated, assets = translate_media_selectors(config, registry)
    selectors = [{key for key in clip if key in {"asset", "asset_id", "media_id", "object_id"}}
                 for clip in translated["clips"]]
    assert selectors == [{"asset"}] * 3
    assert translated["clips"][0]["asset"] == translated["clips"][1]["asset"] == "pic"
    assert assets["assets"][translated["clips"][2]["asset"]]["content_sha256"] == "b" * 64
    assert config["clips"][0] == {"id": "a", "asset_id": "pic"}  # inputs are not changed


def test_unresolvable_selector_is_left_for_the_strict_check():
    config = {"output": {"fps": 30, "resolution": "1920x1080", "file": "out.mp4"},
              "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}],
              "clips": [{"id": "a", "track": "picture", "clipType": "media", "at": 0, "hold": 1,
                         "asset": "pic", "media_id": TAKE}]}
    registry = {"assets": {"pic": {"media_id": PICTURE}}}
    translated, assets = translate_media_selectors(config, registry, strict=False)
    assert translated["clips"][0]["media_id"] == TAKE
    with pytest.raises(ReaderProjectionError, match="unknown key 'media_id'"):
        check_reader_projection(translated, assets)


# --- the commit gate -----------------------------------------------------

def test_commit_gate_refuses_a_result_no_reader_can_open():
    from runtime_protocol.timeline_commit import CommitRejected

    from astrid.sdk.timeline.commit import _check_readers

    service = create_runtime_commit_service()
    readable_document = _document_of(readable_payload())
    broken = readable_payload()
    broken["clips"][0]["asset"] = "missing-asset"
    unreadable_document = _document_of(broken)
    base = {"revision_id": "r0", "publication_id": "pub0"}
    assert reader_problem(service, readable_document) is None
    problem = reader_problem(service, unreadable_document)
    assert problem is not None and "missing-asset" in problem

    with pytest.raises(CommitRejected) as rejected:
        _check_readers(service, unreadable_document, readable_document, base, base)
    failure = rejected.value.failure
    assert failure["details"]["status"] == "invalid"
    assert failure["details"]["reason"] == "unreadable_result"
    assert "Nothing was committed" in failure["message"] and "missing-asset" in failure["message"]
    # Readable results pass; an already unreadable timeline can still be edited back.
    _check_readers(service, readable_document, readable_document, base, base)
    _check_readers(service, readable_document, unreadable_document, base, base)
    _check_readers(service, unreadable_document, unreadable_document, base, base)


def test_commit_gate_refusal_has_readable_wording():
    from astrid.sdk.timeline._commit_wording import present_failure

    presented = present_failure({"code": "validation_error", "message": "Refused.",
                                 "details": {"status": "invalid", "reason": "unreadable_result"}})
    assert presented["headline"].startswith("Render and visualize could not open the timeline")
    assert "clip.asset" in presented["fix"]


def test_commit_gate_view_matches_what_render_reads():
    from astrid.core.timeline.reader_projection import reader_view
    from astrid.packs.rendering.actions.render.managed_timeline import _render_compatible_projection

    config = {"clips": [
        {"id": "a", "asset": "pic", "rect": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}},
        {"id": "b", "media_id": TAKE},
    ]}
    registry = {"assets": {"pic": {"media_id": PICTURE, "media_type": "video/mp4", "origin": "managed-local"}}}
    def without_annotations(view):
        view_config, view_registry = view
        return [{key: value for key, value in clip.items() if key != "app"}
                for clip in view_config["clips"]], view_registry

    assert without_annotations(reader_view(config, registry)) == without_annotations(
        _render_compatible_projection(config, registry))


def test_created_track_without_a_label_is_labelled_with_its_id_and_passes_the_gate():
    from astrid.sdk.timeline.capture import capture_track_create

    service = create_runtime_commit_service()
    captured = capture_track_create(readable_bundle(), "parent", {"id": "overlay", "kind": "visual"})
    assert '"label": "overlay"' in json.dumps(captured, default=str)
    named = capture_track_create(readable_bundle(), "parent",
                                 {"id": "named", "kind": "visual", "label": "Named"})
    assert '"label": "Named"' in json.dumps(named, default=str)

    # The result the commit produces: the new track as the writer labelled it.
    payload = readable_payload()
    payload["config"]["tracks"].append({"id": "overlay", "kind": "visual", "label": "overlay"})
    assert reader_problem(service, _document_of(payload)) is None
    payload["config"]["tracks"][-1].pop("label")
    legacy = _document_of(payload)
    assert reader_problem(service, legacy) is None
    projected = service.project_capture(legacy, normalize_appearance=False)
    overlay = next(track for track in projected["config"]["tracks"] if track["id"] == "overlay")
    assert overlay["label"] == overlay["id"]

    payload["config"]["tracks"][-1]["label"] = None
    assert "is not of type 'string'" in reader_problem(service, _document_of(payload))


def test_reader_gate_rediscovers_pack_added_after_registry_was_cached(tmp_path, monkeypatch):
    import shutil
    from concurrent.futures import ThreadPoolExecutor

    from astrid.core.element import catalog
    from astrid.core.element import registry as element_registry
    from tests.sdk.test_timeline_element_schema_resolution import ELEMENT, PACK, _probe_pack
    from runtime_protocol.timeline_commit import CommitRejected
    from astrid.sdk.timeline.commit import _check_readers

    monkeypatch.setenv("ASTRID_PACKS_PATH", str(tmp_path))
    catalog._clear_registry_cache()
    service = create_runtime_commit_service()
    baseline = _document_of(readable_payload())
    payload = readable_payload()
    payload["clips"][0].update(clipType=ELEMENT, params={"size": 2})
    candidate = _document_of(payload)
    invalid_payload = readable_payload()
    invalid_payload["clips"][0].update(clipType=ELEMENT, params={"size": "bad"})
    invalid = _document_of(invalid_payload)
    base = {"revision_id": "r0", "publication_id": "pub0"}
    try:
        # With no schema, the reader leaves an unknown effect opaque, even when
        # its params would be invalid under the schema installed below.
        assert reader_problem(service, baseline) is None
        assert reader_problem(service, invalid) is None
        with pytest.raises(KeyError, match=ELEMENT):
            catalog.read_effect_schema(ELEMENT)
        before_install = element_registry.load_default_registry()
        with pytest.raises(KeyError, match=ELEMENT):
            before_install.get("effects", ELEMENT)

        _probe_pack(tmp_path)
        # Immediately exercise the exact reader gate used by a commit after the
        # pack appears in the same process.
        assert reader_problem(service, candidate) is None
        _check_readers(service, candidate, baseline, base, base)
        with pytest.raises(CommitRejected, match="'bad' is not of type 'integer'.*Nothing was committed"):
            _check_readers(service, invalid, baseline, base, base)

        with pytest.raises(KeyError, match=ELEMENT):
            before_install.get("effects", ELEMENT)
        assert ELEMENT in catalog.list_effect_ids()
        assert catalog.read_effect_schema(ELEMENT)["properties"]["size"]["type"] == "integer"
        installed = element_registry.load_default_registry()
        definition = installed.get("effects", ELEMENT, pack_id=PACK)
        assert definition.component.is_file()
        revision = catalog._element_revision(definition)
        assert installed.get("effects", ELEMENT, pack_id=PACK, revision=revision) == definition

        with pytest.raises(KeyError, match="wrong-revision"):
            installed.get("effects", ELEMENT, pack_id=PACK, revision="wrong-revision")

        # Both the catalog and the raw registry share the cached corpus. Once
        # this stable installed tree is loaded, concurrent readers and later hits
        # should reuse the same discovery.
        catalog._clear_registry_cache()
        discoveries = []
        original_discover = element_registry.discover_pack_metadata

        def count_discovery(*args, **kwargs):
            discoveries.append(1)
            return original_discover(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(element_registry, "discover_pack_metadata", count_discovery)

            def catalog_lookup():
                assert ELEMENT in catalog.list_effect_ids()
                return catalog.read_effect_schema(ELEMENT)["properties"]["size"]["type"]

            def registry_lookup():
                registry = element_registry.load_default_registry()
                return registry.get("effects", ELEMENT, pack_id=PACK).component.is_file()

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda fn: fn(), [catalog_lookup, registry_lookup] * 8))
        assert results == ["integer", True] * 8
        assert len(discoveries) == 1

    finally:
        catalog._clear_registry_cache()
