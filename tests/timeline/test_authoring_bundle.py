from __future__ import annotations



import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrid.core.timeline.authoring_bundle import (
    AuthoringBundleError,
    UnsupportedAuthoringEditError,
    approve_authoring_candidate,
    authoring_contract,
    authoring_media_inventory,
    compile_authoring_candidate,
    diff_authoring_candidate,
    format_authoring_inspection,
    inspect_authoring_candidate,
    open_authoring_bundle,
    preview_authoring_candidate,
    publish_authoring_candidate,
    validate_authoring_candidate,
)
from astrid.core.timeline.shot_composition_projection import (
    ShotCompositionProjectionError,
    project_runtime_parent_composition,
)
from astrid.core.timeline.validators.meta import MetaValidationError
from astrid.packs.rendering.actions.render.managed_timeline import ManagedRenderSnapshot
from astrid.sdk.authoring_render_preview import (
    candidate_preview_snapshot,
    render_authoring_candidate_preview,
)
from astrid.sdk.invocation import _prepare_managed_render_inputs
from astrid.sdk.timeline_editing import (
    add_track,
    duplicate_authoring_shot,
    move_occurrence_group,
    place_media,
    remove_authoring_shot,
)

OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64
AUDIO = "sha256:" + "3" * 64
PHANTOM = "sha256:" + "9" * 64


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _closure(*, shared=True):
    internal_payload = {
        "tracks": [
            {"id": "picture", "kind": "visual", "opaque_track": {"future": True}},
            {"id": "voice", "kind": "audio"},
        ],
        "clips": [
            {
                "id": "picture-1",
                "track": "picture",
                "clipType": "media",
                "asset": "old-picture",
                "at": 0,
                "hold": 1,
                "rect": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
                "opaque_clip": {"literal": "item-1"},
            },
            {
                "id": "voice-1",
                "track": "voice",
                "clipType": "media",
                "asset": "voice",
                "at": 0,
                "hold": 1,
            },
        ],
        "effects": [{"type": "opacity", "value": 0.75}],
        "audio": [{"track_id": "voice", "gain": 0.8}],
        "layout": {"width": 1920, "height": 1080},
        "registry": {
            "assets": {
                "old-picture": {"media_id": OLD, "type": "image", "opaque_asset": "keep"},
                "voice": {"media_id": AUDIO, "type": "audio"},
            },
            "opaque_registry": {"keep": [1, 2, 3]},
        },
        "assets": [],
        "opaque_timeline": {"future_schema": 7},
    }
    shot_payload = {
        "name": "Opening",
        "metadata": {"settings": {"look": "warm"}, "opaque": ["keep"]},
        "items": [
            {
                "item_id": "item-1",
                "media_id": OLD,
                "source_frame": None,
                "metadata": {
                    "role": "primary_visual",
                    "source_item_id": "item-1",
                    "opaque_literal": "item-1",
                },
            },
            {
                "item_id": "item-audio",
                "media_id": AUDIO,
                "source_frame": None,
                "metadata": {"role": "voiceover"},
            },
        ],
        "pools": [{"id": "pool-1", "opaque": "keep"}],
        "selected_variants": {"pool-1": "variant-1"},
        "provenance": {"source": "fixture", "shot_id_literal": "shot-shared"},
        "generation_inputs": {"visual": {"object_id": OLD}},
        "audio_bindings": [{"track_id": "voice", "object_id": AUDIO}],
        "text_bindings": [{"slot": "voiceover", "text": "hello"}],
        "internal_timeline_revision_id": "internal-1",
        "opaque_shot": {"future_schema": 9},
    }
    occurrences = [
        {
            "occurrence_id": "occ-1",
            "shot_id": "shot-shared",
            "shot_revision_id": "shot-rev-1",
            "placement": {"start_ms": 0, "track": "picture", "opaque_geometry": "keep"},
            "source_offset": {"start": 0, "end": 1000},
            "duration_ms": 1000,
            "speed": {"numerator": 1, "denominator": 1},
            "track": "picture",
            "transform": {"scale": 1, "opaque_transform": [4, 5]},
            "gain": 1,
            "muted": False,
            "provenance": {"placement": "first"},
            "future_occurrence": {"round_trip": True},
        }
    ]
    if shared:
        second = copy.deepcopy(occurrences[0])
        second["occurrence_id"] = "occ-2"
        second["placement"]["start_ms"] = 1000
        second["provenance"]["placement"] = "second"
        occurrences.append(second)
    parent_payload = {
        "config": {
            "tracks": [{"id": "picture", "kind": "visual"}],
            "clips": [{"id": "direct", "track": "picture", "asset": "direct", "at": 3, "hold": 1}],
            "opaque_parent_config": {"keep": True},
        },
        "registry": {"assets": {"direct": {"media_id": OLD}}},
        "clips": [{"id": "direct", "track": "picture", "asset": "direct", "at": 3, "hold": 1}],
        "occurrences": occurrences,
        "opaque_parent": {"keep": ["all", "fields"]},
    }
    parent = {
        "revision_id": "parent-1",
        "project_id": "project-1",
        "timeline_id": "main",
        "content_digest": _digest(parent_payload),
        "payload": parent_payload,
    }
    shot = {
        "revision_id": "shot-rev-1",
        "project_id": "project-1",
        "shot_id": "shot-shared",
        "internal_timeline_revision_id": "internal-1",
        "content_digest": _digest(shot_payload),
        "payload": shot_payload,
    }
    internal = {
        "revision_id": "internal-1",
        "project_id": "project-1",
        "timeline_id": "main",
        "content_digest": _digest(internal_payload),
        "payload": internal_payload,
    }
    return parent, [shot], [internal]


def _runtime_document(parent, shots, internals):
    """Build the immutable closure image consumed by the v2 test port."""
    def media_ids(value):
        if isinstance(value, dict):
            return {media_id for child in value.values() for media_id in media_ids(child)}
        if isinstance(value, list):
            return {media_id for child in value for media_id in media_ids(child)}
        if isinstance(value, str) and value.startswith("sha256:"):
            return {value}
        return set()

    parent_image = {key: copy.deepcopy(parent[key]) for key in ("revision_id", "content_digest", "payload")}
    shot_images = [{key: copy.deepcopy(row[key]) for key in (
        "shot_id", "revision_id", "internal_timeline_revision_id", "content_digest", "payload"
    )} for row in shots]
    internal_images = [{key: copy.deepcopy(row[key]) for key in (
        "timeline_id", "revision_id", "content_digest", "payload"
    )} for row in internals]
    media = sorted(
        ({"media_id": media_id, "content_digest": media_id}
         for media_id in media_ids([parent_image, *shot_images, *internal_images])),
        key=lambda row: row["media_id"],
    )
    return {
        "schema_version": 1,
        "project_id": parent["project_id"],
        "timeline_id": parent["timeline_id"],
        "parent_revision": parent_image,
        "shot_revisions": sorted(shot_images, key=lambda row: (row["shot_id"], row["revision_id"])),
        "internal_timeline_revisions": sorted(
            internal_images, key=lambda row: (row["timeline_id"], row["revision_id"])
        ),
        "dependency_manifest": {
            "shots": sorted(({
                key: row[key] for key in (
                    "shot_id", "revision_id", "internal_timeline_revision_id", "content_digest"
                )
            } for row in shot_images), key=lambda row: (row["shot_id"], row["revision_id"])),
            "internal_timelines": sorted(({
                key: row[key] for key in ("timeline_id", "revision_id", "content_digest")
            } for row in internal_images), key=lambda row: (row["timeline_id"], row["revision_id"])),
            "media": media,
            "documents": [],
        },
    }


def _replace_selected_media(bundle, shot_id):
    shot = bundle["shots"][shot_id]
    shot["payload"]["items"][0]["media_id"] = NEW
    shot["internal_timeline"]["clips"][0]["asset"] = "new-picture"
    shot["internal_timeline"]["registry"]["assets"]["new-picture"] = {
        "media_id": NEW,
        "type": "image",
        "opaque_asset": "new",
    }


def _replace_values(value, replacements):
    if isinstance(value, dict):
        return {key: _replace_values(child, replacements) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_values(child, replacements) for child in value]
    return replacements.get(value, value)


def test_open_is_lossless_read_only_and_independent_for_shared_placements():
    parent, shots, timelines = _closure(shared=True)
    before = copy.deepcopy((parent, shots, timelines))

    bundle = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )

    assert (parent, shots, timelines) == before
    assert len(bundle["shots"]) == 2
    assert bundle["placements"][0]["shot_id"] != bundle["placements"][1]["shot_id"]
    first, second = [bundle["shots"][row["shot_id"]] for row in bundle["placements"]]
    assert first["payload"]["items"][0]["item_id"] != second["payload"]["items"][0]["item_id"]
    assert (
        first["payload"]["items"][0]["metadata"]["source_item_id"]
        == first["payload"]["items"][0]["item_id"]
    )
    # Opaque literal strings are not guessed to be references.
    assert first["payload"]["items"][0]["metadata"]["opaque_literal"] == "item-1"
    assert first["payload"]["opaque_shot"] == {"future_schema": 9}
    assert first["internal_timeline"]["opaque_timeline"] == {"future_schema": 7}
    assert bundle["parent"]["opaque_parent"] == {"keep": ["all", "fields"]}
    assert bundle["placements"][0]["future_occurrence"] == {"round_trip": True}

    contract = authoring_contract()
    assert contract["timing"]["intervals"] == "half-open [start,end)"
    assert "source-to-authoring identity mapping" in contract["derived"]
    assert "transform" in contract["capabilities"]["placement"]["editable"]
    assert "source_offset" in contract["capabilities"]["placement"]["guarded"]


def test_compile_materializes_shared_identity_and_keeps_untouched_content():
    parent, shots, timelines = _closure(shared=True)
    bundle = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    first_id = bundle["placements"][0]["shot_id"]
    second_id = bundle["placements"][1]["shot_id"]
    _replace_selected_media(bundle, first_id)

    compiled = compile_authoring_candidate(bundle)
    publication = compiled.publication

    assert {row["shot_id"] for row in publication["shot_revisions"]} == {first_id, second_id}
    by_id = {row["shot_id"]: row for row in publication["shot_revisions"]}
    assert by_id[first_id]["payload"]["items"][0]["media_id"] == NEW
    assert by_id[second_id]["payload"]["items"][0]["media_id"] == OLD
    assert by_id[first_id]["payload"]["metadata"] == by_id[second_id]["payload"]["metadata"]
    assert publication["parent_composition"]["config"] == parent["payload"]["config"]
    assert publication["parent_composition"]["opaque_parent"] == parent["payload"]["opaque_parent"]
    assert publication["parent_composition"]["occurrences"][0]["future_occurrence"] == {
        "round_trip": True
    }
    assert publication["parent_composition"]["occurrences"][0]["duration_ms"] == 1000
    assert {row["media_id"] for row in publication["dependency_manifest"]["media"]} == {
        OLD,
        NEW,
        AUDIO,
    }


def test_authored_meta_is_preserved_without_phantom_media_dependencies():
    parent, shots, timelines = _closure(shared=False)
    authored_meta = {"org.example.notes": {"media_id": PHANTOM, "note": "keep"}}
    parent["payload"]["meta"] = authored_meta
    parent["content_digest"] = _digest(parent["payload"])

    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    publication = compile_authoring_candidate(candidate).publication

    assert publication["parent_composition"]["meta"] == authored_meta
    assert PHANTOM not in {
        row["media_id"] for row in publication["dependency_manifest"]["media"]
    }
    assert PHANTOM not in {
        row["media_id"] for row in authoring_media_inventory(candidate)["media"]
    }


def test_component_params_meta_media_is_discovered_but_authored_meta_is_opaque():
    parent, shots, timelines = _closure(shared=False)
    authored_meta = {"org.example.notes": {"media_id": PHANTOM, "note": "keep"}}
    for payload in (parent["payload"], parent["payload"]["config"], timelines[0]["payload"]):
        payload["meta"] = copy.deepcopy(authored_meta)
        for collection in ("tracks", "clips", "occurrences"):
            for row in payload.get(collection, []):
                row["meta"] = copy.deepcopy(authored_meta)
        for asset in payload.get("registry", {}).get("assets", {}).values():
            asset["meta"] = copy.deepcopy(authored_meta)
    shots[0]["payload"]["meta"] = copy.deepcopy(authored_meta)
    for revision in (parent, *shots, *timelines):
        revision["content_digest"] = _digest(revision["payload"])
    original_closure = copy.deepcopy((parent, shots, timelines))

    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    shot = next(iter(candidate["shots"].values()))
    shot["internal_timeline"]["clips"][0]["params"] = {"meta": {"media_id": NEW}}
    original_candidate = copy.deepcopy(candidate)
    publication = compile_authoring_candidate(candidate).publication
    inventory = authoring_media_inventory(candidate)

    assert {row["media_id"] for row in publication["dependency_manifest"]["media"]} == {
        OLD, AUDIO, NEW,
    }
    assert {row["media_id"] for row in inventory["media"]} == {OLD, AUDIO, NEW}
    assert publication["parent_composition"] == {
        **candidate["parent"], "occurrences": publication["parent_composition"]["occurrences"],
    }
    assert publication["parent_composition"]["occurrences"][0]["meta"] == authored_meta
    assert publication["shot_revisions"][0]["payload"]["meta"] == authored_meta
    assert publication["internal_timeline_revisions"][0]["payload"] == shot["internal_timeline"]
    assert candidate == original_candidate
    assert (parent, shots, timelines) == original_closure


def test_authoring_open_and_compile_validate_parent_and_shot_payload_meta():
    parent, shots, timelines = _closure(shared=False)
    parent["payload"]["meta"] = {"org.example.notes": None}
    parent["content_digest"] = _digest(parent["payload"])
    with pytest.raises(MetaValidationError, match="parent_revision.payload.meta"):
        open_authoring_bundle(
            parent, shot_revisions=shots, internal_timeline_revisions=timelines
        )

    parent, shots, timelines = _closure(shared=False)
    shots[0]["payload"]["meta"] = {"org.example.notes": None}
    shots[0]["content_digest"] = _digest(shots[0]["payload"])
    with pytest.raises(MetaValidationError, match=r"shot_revisions\[shot-rev-1\].payload.meta"):
        open_authoring_bundle(
            parent, shot_revisions=shots, internal_timeline_revisions=timelines
        )

    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    candidate["parent"]["meta"] = {"org.example.notes": None}
    with pytest.raises(MetaValidationError, match="candidate.parent.meta"):
        compile_authoring_candidate(candidate)

    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    shot_id = candidate["placements"][0]["shot_id"]
    candidate["shots"][shot_id]["payload"]["meta"] = {"org.example.notes": None}
    with pytest.raises(MetaValidationError, match="candidate.shots.*payload.meta"):
        compile_authoring_candidate(candidate)


@pytest.mark.parametrize("codec", ["item_id", "id", "both", "opaque-id"])
@pytest.mark.parametrize("descriptor_location", ["metadata", "root"])
def test_materialization_remaps_only_item_codec_and_declared_source_links(codec, descriptor_location):
    parent, shots, timelines = _closure(shared=True)
    payload = shots[0]["payload"]
    opaque = {"org.example.notes/v1": {
        "item_id": "item-1", "id": "item-1", "source_item_id": "item-1",
        "source_item": "item-audio", "foreign_item_id": "item-audio",
        "foreign_item_ids": ["item-audio", None, "item-1", "item-audio"],
        "selected_item_id": "item-1", "parent_item_id": "item-audio",
        "item-1": None, "literal": "item-1", "nested": [{"item_id": "item-1"}],
    }}
    for field in ("data", "app", "opaque", "metadata", "provenance", "selected_variants",
                  "generation_inputs"):
        payload[field] = copy.deepcopy(opaque)
    for field in ("pools", "audio_bindings", "text_bindings"):
        payload[field] = [copy.deepcopy(opaque)]
    for item in payload["items"]:
        if codec in ("id", "both", "opaque-id"):
            item["id"] = item["item_id"] if codec != "opaque-id" else "opaque-identity"
        if codec == "id":
            del item["item_id"]
        for field in ("data", "app", "opaque"):
            item[field] = copy.deepcopy(opaque)
        item["metadata"] = copy.deepcopy(opaque)
        descriptor = item["metadata"] if descriptor_location == "metadata" else item
        if descriptor_location == "root":
            del item["metadata"]
        descriptor.update(source_item_id="item-1", source_item="item-audio")
    shots[0]["content_digest"] = _digest(payload)
    original = copy.deepcopy((parent, shots, timelines))
    bundle = open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=timelines)
    first = bundle["shots"][bundle["placements"][0]["shot_id"]]
    copied = first["payload"]
    identity = "id" if codec == "id" else "item_id"
    remap = {item[identity]: copied_item[identity]
             for item, copied_item in zip(payload["items"], copied["items"])}
    expected = copy.deepcopy(payload)
    for item in expected["items"]:
        source = item[identity]
        item[identity] = remap[source]
        if codec == "both":
            item["id"] = remap[source]
        descriptor = item["metadata"] if descriptor_location == "metadata" else item
        descriptor.update(source_item_id=remap["item-1"], source_item=remap["item-audio"])
    assert copied == expected
    assert all(source != target for source, target in remap.items())
    assert (parent, shots, timelines) == original
    first["internal_timeline"]["clips"][0]["at"] = 0.25
    publication = compile_authoring_candidate(bundle).publication
    materialized = next(row for row in publication["shot_revisions"] if row["shot_id"] == first["shot_id"])
    # The compiler owns only this derived child revision link.
    expected["internal_timeline_revision_id"] = materialized["internal_timeline_revision_id"]
    assert materialized["payload"] == expected
    assert (parent, shots, timelines) == original


def test_independent_unique_shot_reuses_untouched_revision_and_direct_edits_compile_identically():
    parent, shots, timelines = _closure(shared=False)
    helper_candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    direct_candidate = copy.deepcopy(helper_candidate)
    shot_id = helper_candidate["placements"][0]["shot_id"]
    _replace_selected_media(helper_candidate, shot_id)
    _replace_selected_media(direct_candidate, shot_id)

    helper = compile_authoring_candidate(helper_candidate)
    direct = compile_authoring_candidate(direct_candidate)

    assert helper.as_dict() == direct.as_dict()
    assert len(helper.changed_identities) == 1
    unchanged = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    reused = compile_authoring_candidate(unchanged)
    assert reused.publication["shot_revisions"] == []
    assert reused.publication["internal_timeline_revisions"] == []
    assert reused.reused_identities[0]["shot_revision_id"] == "shot-rev-1"


def test_reopen_materialized_candidate_keeps_independent_shot_ids_stable():
    parent, shots, timelines = _closure(shared=True)
    first = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    compiled = compile_authoring_candidate(first)
    publication = compiled.publication
    parent_row = {
        "revision_id": publication["parent_revision_id"],
        "project_id": publication["project_id"],
        "timeline_id": publication["timeline_id"],
        "content_digest": publication["content_digest"],
        "payload": publication["parent_composition"],
    }
    shot_rows = [
        {
            "project_id": publication["project_id"],
            **row,
        }
        for row in publication["shot_revisions"]
    ]
    internal_rows = [
        {
            "project_id": publication["project_id"],
            **row,
        }
        for row in publication["internal_timeline_revisions"]
    ]

    reopened = open_authoring_bundle(
        parent_row,
        shot_revisions=shot_rows,
        internal_timeline_revisions=internal_rows,
    )

    assert [row["shot_id"] for row in reopened["placements"]] == [
        row["shot_id"] for row in first["placements"]
    ]
    assert compile_authoring_candidate(reopened).publication["shot_revisions"] == []


def test_timing_is_admitted_but_competing_selected_media_fails_before_publish():
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    shot_id = candidate["placements"][0]["shot_id"]
    candidate["shots"][shot_id]["internal_timeline"]["clips"][0]["hold"] = 2
    compiled = compile_authoring_candidate(candidate)
    assert compiled.publication["shot_revisions"]

    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    candidate["shots"][shot_id]["payload"]["items"][0]["media_id"] = NEW
    with pytest.raises(AuthoringBundleError, match="competing selected media"):
        compile_authoring_candidate(candidate)


def test_target_bound_transport_key_normalizes_digest_prefix_and_punctuation():
    from astrid.sdk.authoring_remote import _transport_idempotency_key

    digest = "sha256:" + "a" * 64
    assert _transport_idempotency_key(digest) == "candidate-" + "a" * 64
    assert _transport_idempotency_key("  edit/key with spaces  ") == "edit-key-with-spaces"
    assert _transport_idempotency_key("!leading") == "candidate--leading"


class _Writer:
    def __init__(self, document):
        self.calls = []
        self.document = document

    def list_timeline_commits(self, project_id, timeline_id, *, cursor=None, limit=100):
        return {"data": {"items": [{
            "publication_id": "publication-1",
            "revision_id": "parent-1",
            "receipt": {
                "receipt_id": "publication-1",
                "result": {"canonical_document": copy.deepcopy(self.document)},
            },
        }], "next_cursor": None}}

    def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
        return {"data": {
            **copy.deepcopy(self.document["parent_revision"]),
            "project_id": project_id,
            "timeline_id": timeline_id,
            "dependency_manifest": copy.deepcopy(self.document["dependency_manifest"]),
        }}

    def get_project_shot_revision(self, project_id, shot_id, revision):
        row = next(row for row in self.document["shot_revisions"] if row["revision_id"] == revision)
        return {"data": {**copy.deepcopy(row), "project_id": project_id}}

    def get_project_timeline_revision(self, project_id, timeline_id, revision):
        row = next(row for row in self.document["internal_timeline_revisions"] if row["revision_id"] == revision)
        return {"data": {**copy.deepcopy(row), "project_id": project_id}}

    def get_project_media_selection(self, project_id, selection):
        raise AssertionError(f"unexpected fixture media selection: {selection}")

    def commit_timeline(self, project_id, timeline_id, body, *, idempotency_key):
        self.calls.append((project_id, timeline_id, body, idempotency_key))
        return {"data": {
            "status": "accepted",
            "accepted_revision_id": "saved-revision",
            "previous_revision_id": "parent-1",
            "canonical_document": copy.deepcopy(self.document),
        }, "receipt": {"receipt_id": "saved-receipt"}}


class _RuntimeAuthoringTransport:
    """Expose the authenticated Runtime reads and commit port to the SDK."""

    def __init__(self, service):
        self.service = service
        self.calls = []

    def list_timeline_commits(self, project_id, timeline_id, **kwargs):
        return self.service.list_timeline_commits(
            project_id, timeline_id, authorize=lambda: None, **kwargs
        )

    def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
        return self.service.get_project_parent_composition_revision(
            project_id, timeline_id, revision
        )

    def get_project_shot_revision(self, project_id, shot_id, revision):
        return self.service.get_project_shot_revision(project_id, shot_id, revision)

    def get_project_timeline_revision(self, project_id, timeline_id, revision):
        return self.service.get_project_timeline_revision(project_id, timeline_id, revision)

    def get_project_media_selection(self, project_id, selection):
        return self.service.get_project_media_selection(
            project_id, selection, authorize=lambda: None
        )

    def commit_timeline(self, project_id, timeline_id, body, *, idempotency_key):
        self.calls.append((project_id, timeline_id, copy.deepcopy(body), idempotency_key))
        return self.service.commit_timeline(
            project_id,
            timeline_id,
            body,
            publisher="author",
            invocation_path=f"/v1/projects/{project_id}/timelines/{timeline_id}/commit",
            authorize=lambda: None,
            idempotency_key=idempotency_key,
        )


def _real_authoring_candidate(tmp_path):
    """Create a real authenticated closure for the authoring commit path."""
    from tests.helpers.runtime import use_runtime
    from tests.helpers.trusted_seed import seed_trusted_parent

    use_runtime()
    from runtime_protocol import service as runtime_service
    from runtime_protocol import store as runtime_store

    from astrid.sdk.timeline.commit import create_runtime_commit_service

    root = tmp_path / "realm"
    runtime_store.RealmStore.initialize(root).close()
    service = runtime_service.RuntimeService(root)
    service.register_timeline_commit_provider(create_runtime_commit_service())
    project_id = service.create_project(
        {"slug": "authoring-approval", "name": "Authoring approval"},
        idempotency_key="project",
    )["id"]
    service.create_timeline(project_id, "main", idempotency_key="timeline")
    old = service.ingest(
        project_id, b"old-picture", media_type="image/png", idempotency_key="old"
    )["data"]["object_id"]
    audio = service.ingest(
        project_id, b"voiceover", media_type="audio/wav", idempotency_key="audio"
    )["data"]["object_id"]

    parent, shots, timelines = _closure(shared=True)
    shots[0]["payload"]["text_bindings"] = []
    replacements = {OLD: old, AUDIO: audio, "project-1": project_id}
    parent = _replace_values(parent, replacements)
    shots = _replace_values(shots, replacements)
    timelines = _replace_values(timelines, replacements)
    for row in (parent, shots[0], timelines[0]):
        row["content_digest"] = _digest(row["payload"])
    seed_trusted_parent(
        service,
        {
            "project_id": project_id,
            "timeline_id": "main",
            "expected_head": None,
            "parent_revision_id": "parent-1",
            "parent_composition": parent["payload"],
            "internal_timeline_revisions": timelines,
            "shot_revisions": shots,
        },
        idempotency_key="seed-parent",
    )

    parent_revision = service.get_project_parent_composition_revision(
        project_id, "main", "parent-1"
    )
    shot_revision = service.get_project_shot_revision(
        project_id, "shot-shared", "shot-rev-1"
    )
    internal_revision = service.get_project_timeline_revision(
        project_id, "main", "internal-1"
    )
    candidate = open_authoring_bundle(
        parent_revision,
        shot_revisions=[shot_revision],
        internal_timeline_revisions=[internal_revision],
    )
    return service, candidate, _RuntimeAuthoringTransport(service)


def test_publish_uses_one_compilation_and_returns_complete_mapping():
    parent, shots, timelines = _closure(shared=True)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    candidate["source_mapping"]["_runtime_document"] = _runtime_document(parent, shots, timelines)
    writer = _Writer(candidate["source_mapping"]["_runtime_document"])

    result = publish_authoring_candidate(candidate, writer, idempotency_key="candidate-1")

    assert len(writer.calls) == 1
    request = writer.calls[0][2]
    assert writer.calls[0][3] == "candidate-1"
    assert request["base_revision_id"] == "parent-1"
    assert request["merge"] == "atomic"
    assert all(command["op"] in {"doc.patch", "media.admit"} for command in request["commands"])
    assert result["publication"]["new_head"] == "saved-revision"
    assert result["publication"]["canonical_document"] == writer.document
    assert set(result["identity_mapping"]["placements"]) == {"occ-1", "occ-2"}
    assert result["summary"]["new_head"] == result["publication"]["data"]["new_head"]
    assert isinstance(result["update"], str)
    assert [action["scope"] for action in result["next_actions"]] == [
        "saved_revision_pair", "saved_revision_pair", "current_head", "current_head",
    ]
    saved_actions = result["next_actions"][:2]
    assert [(action["from_revision"], action["to_revision"], action["format"])
            for action in saved_actions] == [
        ("parent-1", "saved-revision", "readable"),
        ("parent-1", "saved-revision", "details"),
    ]
    assert all("from_revision" not in action for action in result["next_actions"][2:])


def test_publication_data_projects_accepted_parent_revision_without_rewriting_reply():
    parent, shots, timelines = _closure(shared=True)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    candidate["source_mapping"]["_runtime_document"] = _runtime_document(parent, shots, timelines)

    class CompleteReplyWriter(_Writer):
        def commit_timeline(self, project_id, timeline_id, body, *, idempotency_key):
            envelope = super().commit_timeline(
                project_id, timeline_id, body, idempotency_key=idempotency_key
            )
            envelope["data"].update(
                base_revision_id=body["base_revision_id"],
                base_publication_id=body["base_publication_id"],
                commands=copy.deepcopy(body["commands"]),
                steps=copy.deepcopy(body["steps"]),
                merge_mode=body["merge"],
                label=body["label"],
                source=copy.deepcopy(body["source"]),
                rationale=body["rationale"],
                publisher="test-user",
            )
            envelope["receipt"]["result"] = copy.deepcopy(envelope["data"])
            return envelope

    writer = CompleteReplyWriter(candidate["source_mapping"]["_runtime_document"])

    result = publish_authoring_candidate(candidate, writer, idempotency_key="accepted-parent-alias")

    publication = result["publication"]
    assert publication["accepted_revision_id"] == "saved-revision"
    assert publication["parent_revision_id"] == publication["accepted_revision_id"]
    assert publication["data"]["accepted_revision_id"] == "saved-revision"
    assert publication["data"]["parent_revision_id"] == "saved-revision"
    assert publication["canonical_document"] == writer.document
    assert publication["data"]["canonical_document"] == writer.document
    assert publication["receipt"]["result"]["accepted_revision_id"] == "saved-revision"
    assert publication["data"]["steps"] == writer.calls[0][2]["steps"]


def test_group_move_compiles_and_uses_existing_publication_boundary():
    parent, shots, timelines = _closure(shared=True)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    move_occurrence_group(candidate, "occ-2", before_occurrence_id="occ-1")
    compiled = compile_authoring_candidate(candidate).publication
    occurrences = compiled["parent_composition"]["occurrences"]
    assert [row["occurrence_id"] for row in occurrences] == ["occ-2", "occ-1"]
    assert [row["placement"]["start_ms"] for row in occurrences] == [0, 1000]
    assert [row["duration_ms"] for row in occurrences] == [1000, 1000]
    candidate["source_mapping"]["_runtime_document"] = _runtime_document(parent, shots, timelines)
    writer = _Writer(candidate["source_mapping"]["_runtime_document"])

    result = publish_authoring_candidate(candidate, writer, idempotency_key="a03-group-move")

    assert len(writer.calls) == 1
    assert writer.calls[0][3] == "a03-group-move"
    request = writer.calls[0][2]
    assert request["base_revision_id"] == "parent-1"
    assert request["merge"] == "atomic"
    assert all(command["op"] == "doc.patch" for command in request["commands"])
    assert result["publication"]["new_head"] == "saved-revision"


def test_frozen_render_restart_is_single_generation(tmp_path):
    """Exercise the live render contract and durable replay seam together."""
    from astrid.core.execution.reconciler import ExecutionReconciler
    from astrid.core.execution.target_adapter import LocalMachineTargetAdapter

    source = b"deterministically extracted source bytes"
    source_digest = "sha256:" + hashlib.sha256(source).hexdigest()
    contract = {
        "schema_version": 1,
        "workflow": {
            "id": "render.authoring-candidate",
            "contract_digest": "sha256:workflow",
            "required_bindings": ["source_video"],
        },
        "inputs": [
            {
                "name": "source_video",
                "object_id": source_digest,
                "filename": "source.mp4",
                "required": True,
            }
        ],
        "target": {"kind": "machine", "id": "local-1"},
        "retry_policy": {"max_attempts": 2},
        "checks": {"outputs": ["sha256"]},
    }

    class Runtime:
        def __init__(self):
            self.task = None
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(("create", kwargs))
            if self.task is None:
                self.task = {
                    "task_id": "render-task-1",
                    "run_id": "render-run-1",
                    "state": "queued",
                    "idempotency_key": kwargs["idempotency_key"],
                    "capability_id": kwargs["capability"],
                    "spec": {**kwargs["spec"], "execution_request": kwargs["execution_request"]},
                }
            return {"task_id": self.task["task_id"], "run_id": self.task["run_id"]}

        def get_task(self, task_id):
            assert task_id == self.task["task_id"]
            return dict(self.task)

        def claim(self, **kwargs):
            self.calls.append(("claim", kwargs))
            self.task.update({"state": "running", "attempt_id": "render-attempt-1"})
            return {
                "task_id": self.task["task_id"],
                "attempt_id": "render-attempt-1",
                "lease_id": "render-lease-1",
                "fence": 1,
                "runtime_epoch": 1,
                "idempotency_key": "render-request-1",
                "capability_id": "render.authoring-candidate",
                "spec": self.task["spec"],
            }

        def settle(self, attempt_id, **kwargs):
            self.calls.append(("settle", kwargs))
            self.task.update({"state": "completed", "result": {"outputs": kwargs["outputs"]}})
            return {"ok": True}

        def fail(self, *args, **kwargs):
            self.calls.append(("fail", kwargs))
            self.task["state"] = "failed"
            return {"ok": True}

    runtime = Runtime()
    adapter = LocalMachineTargetAdapter(
        {"kind": "machine", "id": "local-1"},
        observer=lambda: {
            "kind": "machine",
            "target_id": "local-1",
            "live": True,
            "runtime_epoch": 1,
            "launch_generation": "launch-1",
            "process_birth_id": "process-1",
            "engine_birth_id": "engine-1",
            "output_root": str(tmp_path),
        },
    )
    render_count = 0

    def execute(_claim, observation):
        nonlocal render_count
        render_count += 1
        output = tmp_path / "render.bin"
        output.write_bytes(source + b"/rendered")
        data = output.read_bytes()
        return {
            "outputs": [
                {
                    "path": output.name,
                    "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            ],
            "source_digest": source_digest,
            "source_filename": "source.mp4",
            "output_root": observation.output_root,
        }

    reconciler = ExecutionReconciler(runtime, adapter)
    first = reconciler.run(
        contract=contract,
        project_id="project-1",
        capability="render.authoring-candidate",
        spec={"inputs": {}},
        executor_id="worker-1",
        execute=execute,
        idempotency_key="render-request-1",
    )
    second = reconciler.run(
        contract=contract,
        project_id="project-1",
        capability="render.authoring-candidate",
        spec={"inputs": {}},
        executor_id="worker-1",
        execute=execute,
        idempotency_key="render-request-1",
    )

    assert first.status == "complete"
    assert second.status == "complete"
    assert second.phase == "recovery"
    assert render_count == 1
    assert [name for name, _ in runtime.calls].count("claim") == 1

def test_full_authored_field_edits_compile_without_normalizing_opaque_fields():
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    shot_id = candidate["placements"][0]["shot_id"]
    candidate["parent"]["config"]["tracks"].append({"id": "effects", "kind": "overlay"})
    placement = candidate["placements"][0]
    placement["placement"]["start_ms"] = 250
    placement["duration_ms"] = 1500
    placement["transform"] = {"x": 100, "y": 0, "width": 960, "height": 540}
    timeline = candidate["shots"][shot_id]["internal_timeline"]
    add_track(candidate["shots"][shot_id], kind="overlay", track_id="overlay")
    timeline["clips"][0]["at"] = 125
    timeline["clips"][0]["hold"] = 3
    timeline["effects"].append({"type": "blur", "value": 0.1})
    timeline["opaque_timeline"]["added"] = {"preserve": True}

    compiled = compile_authoring_candidate(candidate)
    output = compiled.publication["parent_composition"]
    assert output["occurrences"][0]["duration_ms"] == 1500
    child = compiled.publication["shot_revisions"][0]
    assert child["payload"]["opaque_shot"] == {"future_schema": 9}
    assert child["payload"]["internal_timeline_revision_id"]
    internal = compiled.publication["internal_timeline_revisions"][0]["payload"]
    assert internal["opaque_timeline"]["added"] == {"preserve": True}
    assert {row["id"] for row in internal["tracks"]} == {"picture", "voice", "overlay"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_offset", {"start": 100, "end": 900}),
        ("speed", 1.5),
        ("transform", {"scale": 1.25}),
    ],
)
def test_placement_edits_without_shared_projection_semantics_fail_before_publish(field, value):
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    candidate["placements"][0][field] = value
    with pytest.raises(UnsupportedAuthoringEditError, match=field):
        compile_authoring_candidate(candidate)


def test_new_placement_is_checked_even_with_self_pinned_source_mapping():
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    added = copy.deepcopy(candidate["placements"][0])
    added["occurrence_id"] = "new-occurrence"
    added["source_offset"] = {"start": 0, "end": 0}
    added["speed"] = 1.5
    candidate["placements"].append(added)
    candidate["source_mapping"]["placements"]["new-occurrence"] = copy.deepcopy(added)

    with pytest.raises(UnsupportedAuthoringEditError, match="speed"):
        compile_authoring_candidate(candidate)


def test_conflicting_parent_registry_locations_rejected_by_compiler():
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    candidate["parent"]["config"]["registry"] = {"assets": {"other": {"media_id": NEW}}}

    with pytest.raises(AuthoringBundleError, match="conflicts"):
        compile_authoring_candidate(candidate)

    parent["payload"]["config"]["registry"] = candidate["parent"]["config"]["registry"]
    with pytest.raises(ShotCompositionProjectionError, match="conflicts"):
        project_runtime_parent_composition(
            parent, shot_revisions=shots, internal_timeline_revisions=timelines
        )


def test_live_runtime_bare_item_media_id_is_accepted_without_rewriting_payload():
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    shot_id = candidate["placements"][0]["shot_id"]
    candidate["shots"][shot_id]["payload"]["items"][0]["media_id"] = OLD.removeprefix("sha256:")

    compiled = compile_authoring_candidate(candidate)
    item = compiled.publication["shot_revisions"][0]["payload"]["items"][0]
    assert item["media_id"] == OLD.removeprefix("sha256:")
    assert {row["media_id"] for row in compiled.publication["dependency_manifest"]["media"]} == {
        OLD,
        AUDIO,
    }


def test_live_source_asset_authority_resolves_child_selector_without_local_registry():
    parent, shots, timelines = _closure(shared=False)
    parent["payload"]["registry"] = {
        "assets": {
            "old-picture": {"media_id": OLD, "type": "video"},
            "voice": {"media_id": AUDIO, "type": "audio"},
        }
    }
    timelines[0]["payload"].pop("registry")
    timelines[0]["content_digest"] = _digest(timelines[0]["payload"])
    parent["content_digest"] = _digest(parent["payload"])

    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    compiled = compile_authoring_candidate(candidate)

    internal = candidate["shots"][candidate["placements"][0]["shot_id"]]["internal_timeline"]
    assert "registry" not in internal
    assert internal["clips"][0]["asset"] == "old-picture"
    assert compiled.publication["internal_timeline_revisions"] == []
    assert OLD in {row["media_id"] for row in compiled.publication["dependency_manifest"]["media"]}

    candidate["shots"][candidate["placements"][0]["shot_id"]]["internal_timeline"]["clips"][0]["asset"] = "unadmitted-new-asset"
    with pytest.raises(AuthoringBundleError, match="missing registry asset"):
        compile_authoring_candidate(candidate)

    parent["payload"]["registry"]["assets"].pop("old-picture")
    parent["content_digest"] = _digest(parent["payload"])
    parent_fallback_removed = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    with pytest.raises(AuthoringBundleError, match="missing registry asset"):
        compile_authoring_candidate(parent_fallback_removed)

    local_parent, local_shots, local_timelines = _closure(shared=False)
    local_candidate = open_authoring_bundle(
        local_parent, shot_revisions=local_shots, internal_timeline_revisions=local_timelines
    )
    local_shot = local_candidate["shots"][local_candidate["placements"][0]["shot_id"]]
    local_shot["internal_timeline"]["registry"]["assets"].pop("old-picture")
    with pytest.raises(AuthoringBundleError, match="missing registry asset"):
        compile_authoring_candidate(local_candidate)


def test_bundle_structure_helpers_duplicate_and_remove_independent_placements():
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    source_id = candidate["placements"][0]["shot_id"]
    duplicate = duplicate_authoring_shot(
        candidate, source_id, new_shot_id="shot-copy", occurrence_id="occ-copy", start_ms=2000
    )
    original_items = candidate["shots"][source_id]["payload"]["items"]
    copied_items = duplicate["payload"]["items"]
    assert copied_items[0]["item_id"] != original_items[0]["item_id"]
    assert copied_items[0]["metadata"]["opaque_literal"] == "item-1"
    assert len(candidate["placements"]) == 2
    with pytest.raises(UnsupportedAuthoringEditError, match="source_offset"):
        compile_authoring_candidate(candidate)
    candidate["placements"][1]["source_offset"] = {"start": 0, "end": 0}
    compilation = compile_authoring_candidate(candidate)
    assert {row["shot_id"] for row in compilation.publication["shot_revisions"]} == {"shot-copy"}
    remove_authoring_shot(candidate, "shot-copy")
    assert "shot-copy" not in candidate["shots"]
    assert [row["occurrence_id"] for row in candidate["placements"]] == ["occ-1"]
    assert compile_authoring_candidate(candidate).publication["shot_revisions"] == []



def test_diff_baseline_normalizes_placements_and_rejects_malformed_mapping():
    parent, shots, timelines = _closure(shared=True)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    roundtrip = json.loads(json.dumps(candidate))
    assert diff_authoring_candidate(roundtrip)["change_count"] == 0

    candidate["placements"][0]["duration_ms"] += 100
    timed = diff_authoring_candidate(candidate)
    assert timed["change_count"] > 0
    assert any("duration_ms" in row["path"] for row in timed["changed"])

    candidate["placements"][0]["duration_ms"] -= 100
    candidate["placements"].reverse()
    ordered = diff_authoring_candidate(candidate)
    assert ordered["change_count"] > 0
    assert any("placements" in row["path"] for row in ordered["changed"])

    candidate["source_mapping"]["placements"] = []
    with pytest.raises(AuthoringBundleError, match="candidate.source_mapping.placements"):
        diff_authoring_candidate(candidate)


def test_validate_diff_preview_and_media_inventory_are_read_only_and_frozen():
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    candidate["placements"][0]["duration_ms"] = 1234
    validation = validate_authoring_candidate(candidate)
    diff = diff_authoring_candidate(candidate)
    inventory = authoring_media_inventory(candidate)
    preview = preview_authoring_candidate(candidate)
    assert validation["valid"] is True
    assert validation["candidate_digest"] == preview["candidate_digest"]
    assert diff["change_count"] > 0
    assert {row["media_id"] for row in inventory["media"]} == {OLD, AUDIO}
    assert inventory["composed_ref"]["candidate_digest"] == inventory["candidate_digest"]
    assert all(
        row["source_ref"]["project_id"] == "project-1"
        and row["source_ref"]["digest"] == row["media_id"]
        for row in inventory["media"]
    )
    frozen_digest = preview["candidate_digest"]
    candidate["placements"][0]["duration_ms"] = 9999
    assert preview["candidate_digest"] == frozen_digest
    assert preview["publication"]["parent_composition"]["occurrences"][0]["duration_ms"] == 1234


def test_frozen_candidate_projects_to_labelled_managed_render_without_publication():
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    # This preview exercises supported placement; group transforms are refused.
    candidate["placements"][0]["transform"] = {}
    candidate["placements"][0]["duration_ms"] = 1234
    preview = preview_authoring_candidate(candidate)
    candidate["placements"][0]["duration_ms"] = 9999

    class Runtime:
        media = SimpleNamespace(list=lambda *_args, **_kwargs: SimpleNamespace(
            ok=True, data=[[{"media_id": media_id, "digest": media_id[7:]} for media_id in (OLD, AUDIO)], None]
        ))

        def __init__(self):
            def result(data):
                return SimpleNamespace(ok=True, data=data)

            self.projects = SimpleNamespace(show=lambda _ref: result({"id": "project-1", "slug": "demo"}))
            timeline = {
                "timeline_id": "main", "timeline_ulid": "main", "slug": "main",
                "version": 7, "head_revision_id": "parent-1",
                "config": {"tracks": [], "clips": []}, "registry": {"assets": {}},
            }
            self.timelines = SimpleNamespace(
                show=lambda _project, _ref: result(timeline),
                list=lambda _project, **_kwargs: result([[timeline], None]),
                inspect=lambda _project, _timeline, **_kwargs: result({
                    "representation": "canonical_head",
                    "is_current_head": True,
                    "revision_id": "parent-1",
                    "timeline_id": "main",
                }),
            )

        def get_project_parent_composition_revision(self, _project, _timeline, revision_id):
            assert revision_id == "parent-1"
            return parent

        def get_project_shot_revision(self, _project, shot_id, revision_id):
            return next(row for row in shots if row["shot_id"] == shot_id and row["revision_id"] == revision_id)

        def get_project_timeline_revision(self, _project, _timeline, revision_id):
            return next(row for row in timelines if row["revision_id"] == revision_id)

    snapshot = ManagedRenderSnapshot(
        project_id="project-1", project_slug="demo", timeline_id="main",
        timeline_ulid="main", timeline_slug="main", config_version=7,
        head_event_id="parent-1", head_hash=parent["content_digest"][7:],
        config={"tracks": [], "clips": []}, registry={"assets": {}},
        config_hash="0" * 64, registry_hash="0" * 64,
        materialized_registry_hash="0" * 64,
    )
    projected = candidate_preview_snapshot(preview, snapshot=snapshot, client=Runtime())
    authority = projected.authority()
    assert authority["authority"] == "kernel"
    assert authority["render_mode"] == "authoring_candidate_preview"
    assert authority["authoring_preview"]["label"] == "Unpublished candidate preview"
    assert authority["authoring_preview"]["base_parent"] == preview["head"]
    assert authority["authoring_preview"]["candidate_digest"] == preview["candidate_digest"]
    assert projected.expansion["outputs"][0]["duration_ms"] == 1234
    assert projected.head_event_id == "parent-1"

    render_candidate = copy.deepcopy(preview["candidate"])
    render_candidate["parent"]["config"] = {
        "tracks": [
            {"id": "picture", "kind": "visual", "label": "Picture"},
            {"id": "voice", "kind": "audio", "label": "Voice"},
        ], "clips": [],
    }
    render_candidate["parent"]["clips"] = []
    shot_id = render_candidate["placements"][0]["shot_id"]
    internal = render_candidate["shots"][shot_id]["internal_timeline"]
    internal["tracks"] = copy.deepcopy(render_candidate["parent"]["config"]["tracks"])
    internal["clips"] = []
    picture_clip = place_media(
        internal, OLD, track=internal["tracks"][0], start=0, end=1,
        clip_id="picture-1", fit="cover",
        rect={"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
        clipType="media",
    )
    place_media(
        internal, AUDIO, track=internal["tracks"][1], start=0, end=1,
        clip_id="voice-1", clipType="media",
    )
    assert picture_clip["media_id"] == OLD
    assert picture_clip["fit"] == "cover"
    internal["registry"] = {"assets": {
        "old-picture": {"media_id": OLD, "type": "image", "origin": "managed-local"},
        "voice": {"media_id": AUDIO, "type": "audio", "origin": "managed-local"},
    }}
    render_preview = preview_authoring_candidate(render_candidate)

    class PreviewRuntime(Runtime):
        def get_project_shot_revision(self, *_args):
            raise AssertionError("preview should not project the unrenderable source shot")

        def get_project_timeline_revision(self, *_args):
            raise AssertionError("preview should not project the unrenderable source timeline")

    prepared, admitted = _prepare_managed_render_inputs(
        {"timeline_ref": "main", "authoring_preview": render_preview},
        project="demo", _client=PreviewRuntime(),
    )
    assert "authoring_preview" not in prepared
    assert admitted["authoring_preview"]["candidate_digest"] == render_preview["candidate_digest"]
    assert admitted["authority"] == "kernel"
    assert admitted["render_mode"] == "authoring_candidate_preview"
    assert admitted["config_version"] == 7
    assert admitted["authoring_preview"]["publication_digest"] == render_preview["publication_digest"]
    prepared_config = prepared["timeline_snapshot"]["config"]
    prepared_picture = next(
        clip for clip in prepared_config["clips"] if clip["id"].endswith(":picture-1")
    )
    prepared_track = next(
        track for track in prepared_config["tracks"] if track["id"] == prepared_picture["track"]
    )
    assert prepared_picture["asset"]
    assert "media_id" not in prepared_picture
    assert "fit" not in prepared_picture
    assert "rect" not in prepared_picture
    assert prepared_track["fit"] == "cover"
    assert {key: prepared_picture[key] for key in ("x", "y", "width", "height")} == {
        "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4,
    }
    projection = prepared_picture["app"]["astrid_authoring_render_projection"]
    assert projection["media_selector"]["media_id"] == OLD
    assert projection["fit"] == {"value": "cover", "projected_to_track": prepared_picture["track"]}
    prepared_asset = prepared["timeline_snapshot"]["registry"]["assets"][prepared_picture["asset"]]
    assert prepared_asset["origin"] == "opaque-foreign"

    stale = copy.deepcopy(preview)
    stale["candidate_digest"] = "sha256:" + "0" * 64
    with pytest.raises(AuthoringBundleError, match="frozen candidate"):
        candidate_preview_snapshot(stale, snapshot=snapshot, client=Runtime())
    with pytest.raises(AuthoringBundleError, match="no longer the exact render head"):
        candidate_preview_snapshot(
            preview, snapshot=replace(snapshot, head_event_id="parent-2"), client=Runtime()
        )

    class RenderClient:
        def invoke_result(self, capability_id, **kwargs):
            assert capability_id == "rendering.render"
            assert kwargs["inputs"]["authoring_preview"]["candidate_digest"]
            assert kwargs["inputs"]["review"] is True
            return {"run_id": "render-run-1", "receipt": "managed"}

    assert render_authoring_candidate_preview(
        preview, RenderClient(), project="demo", timeline_ref="main"
    ) == {"run_id": "render-run-1", "receipt": "managed"}


def test_compact_inspection_pins_head_and_reports_bounded_structure_without_opaque_payloads():
    parent, shots, timelines = _closure(shared=True)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    before = copy.deepcopy(candidate)
    inspection = inspect_authoring_candidate(candidate, offset=1, limit=1)

    assert candidate == before
    assert inspection["kind"] == "authoring-candidate-inspection"
    assert inspection["head"] == candidate["base_parent"]
    assert inspection["page"] == {
        "offset": 1,
        "limit": 1,
        "returned": 1,
        "total": 2,
        "next_offset": None,
    }
    assert inspection["placements"][0]["occurrence_id"] == "occ-2"
    assert inspection["counts"] == {
        "placements": 2,
        "shots": 2,
        "changed_shots": 2,
        "media": 2,
        "unresolved": 0,
    }
    assert {row["media_id"] for row in inspection["media"]} == {OLD, AUDIO}
    assert inspection["omissions"] == {"placements": 1}
    assert "opaque_parent" not in json.dumps(inspection)
    text = format_authoring_inspection(inspection)
    assert "head=parent-1" in text
    assert "placement occ-2" in text


@pytest.mark.integration  # a real Runtime from a second checkout
def test_candidate_compiler_integrates_with_runtime_atomic_publication(tmp_path):
    from runtime_protocol import service as runtime_service
    from runtime_protocol import store as runtime_store

    from astrid.sdk.timeline.commit import create_runtime_commit_service
    from tests.helpers.trusted_seed import seed_trusted_parent

    root = tmp_path / "realm"
    runtime_store.RealmStore.initialize(root).close()
    service = runtime_service.RuntimeService(root)
    service.register_timeline_commit_provider(create_runtime_commit_service())

    class Transport:
        def list_timeline_commits(self, project_id, timeline_id, **kwargs):
            return service.list_timeline_commits(project_id, timeline_id, authorize=lambda: None, **kwargs)

        def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
            return service.get_project_parent_composition_revision(project_id, timeline_id, revision)

        def get_project_shot_revision(self, project_id, shot_id, revision):
            return service.get_project_shot_revision(project_id, shot_id, revision)

        def get_project_timeline_revision(self, project_id, timeline_id, revision):
            return service.get_project_timeline_revision(project_id, timeline_id, revision)

        def get_project_media_selection(self, project_id, selection):
            return service.get_project_media_selection(project_id, selection, authorize=lambda: None)

        def commit_timeline(self, project_id, timeline_id, payload, **kwargs):
            return service.commit_timeline(
                project_id, timeline_id, payload, publisher="test-user",
                invocation_path=f"/v1/projects/{project_id}/timelines/{timeline_id}/commit",
                authorize=lambda: None, **kwargs
            )

    transport = Transport()
    try:
        project = service.create_project(
            {"slug": "authoring", "name": "Authoring"}, idempotency_key="project"
        )
        project_id = project["id"]
        service.create_timeline(project_id, "main", idempotency_key="timeline")
        old = service.ingest(
            project_id, b"old-picture", media_type="image/png", idempotency_key="old"
        )["data"]["object_id"]
        new = service.ingest(
            project_id, b"new-picture", media_type="image/png", idempotency_key="new"
        )["data"]["object_id"]
        audio = service.ingest(
            project_id, b"voiceover", media_type="audio/wav", idempotency_key="audio"
        )["data"]["object_id"]
        parent, shots, timelines = _closure(shared=True)
        # Runtime now requires authored shot text to pin a registered binding;
        # this atomic compiler fixture is not testing text registration.
        shots[0]["payload"]["text_bindings"] = []
        replacements = {OLD: old, NEW: new, AUDIO: audio, "project-1": project_id}
        parent = _replace_values(parent, replacements)
        shots = _replace_values(shots, replacements)
        timelines = _replace_values(timelines, replacements)
        timelines[0]["content_digest"] = _digest(timelines[0]["payload"])
        shots[0]["content_digest"] = _digest(shots[0]["payload"])
        parent["content_digest"] = _digest(parent["payload"])
        seed = {
            "project_id": project_id,
            "timeline_id": "main",
            "expected_head": None,
            "parent_revision_id": "parent-1",
            "parent_composition": parent["payload"],
            "internal_timeline_revisions": timelines,
            "shot_revisions": shots,
        }
        seed_trusted_parent(service, seed, idempotency_key="seed-parent")
        exact_parent = service.get_project_parent_composition_revision(
            project_id, "main", "parent-1"
        )
        exact_shot = service.get_project_shot_revision(project_id, "shot-shared", "shot-rev-1")
        exact_internal = service.get_project_timeline_revision(project_id, "main", "internal-1")
        candidate = open_authoring_bundle(
            exact_parent,
            shot_revisions=[exact_shot],
            internal_timeline_revisions=[exact_internal],
        )
        first_shot = candidate["placements"][0]["shot_id"]
        candidate["shots"][first_shot]["payload"]["items"][0]["media_id"] = new
        candidate["shots"][first_shot]["internal_timeline"]["clips"][0]["asset"] = "new-picture"
        candidate["shots"][first_shot]["internal_timeline"]["registry"]["assets"]["new-picture"] = {
            "media_id": new,
            "type": "image",
        }

        committed = publish_authoring_candidate(
            candidate, transport, idempotency_key="complete-candidate"
        )
        new_head = committed["publication"]["data"]["new_head"]
        assert committed["summary"]["new_head"] == new_head
        assert committed["summary"]["catalog_media"] is None
        assert committed["summary"]["media_selections"]["image"]["replaced"] == 1
        assert any(row["scope"] == "saved_revision_pair" for row in committed["next_actions"])
        assert f"Saved revision {new_head}" in committed["update"]
        reread = service.get_project_parent_composition_revision(project_id, "main", new_head)

        assert new_head == committed["publication"]["data"]["parent_revision_id"]
        assert new_head != committed["identity_mapping"]["placements"]["occ-1"]["shot_revision_id"]
        assert (
            reread["payload"]["occurrences"][0]["shot_id"] == next(
                command["args"]["shot_id"]
                for command in committed["publication"]["data"]["commands"]
                if command["op"] == "occurrence.fork"
            )
            and (
                reread["payload"]["occurrences"][1]["shot_id"],
                reread["payload"]["occurrences"][1]["shot_revision_id"],
            ) == ("shot-shared", "shot-rev-1")
        )
        assert reread["payload"]["opaque_parent"] == {"keep": ["all", "fields"]}
        newer_payload = copy.deepcopy(reread["payload"])
        newer_payload["opaque_parent"]["writer"] = "later"
        seed_trusted_parent(
            service,
            {
                "project_id": project_id,
                "timeline_id": "main",
                "expected_head": new_head,
                "parent_revision_id": "newer-head",
                "parent_composition": newer_payload,
            },
            idempotency_key="later-writer",
        )
        replay = publish_authoring_candidate(
            candidate, transport, idempotency_key="complete-candidate"
        )
        assert replay == committed
        assert service._timeline_resource("main")["head_revision_id"] == "newer-head"
    finally:
        service.close()


def test_approval_migration_is_bound_to_candidate_and_base_revision(tmp_path):
    service, candidate, writer = _real_authoring_candidate(tmp_path)
    try:
        approval = approve_authoring_candidate(
            candidate, approver="editor-1", approval_id="approval-1"
        )
        assert approval["base_parent"] == candidate["base_parent"]

        changed = copy.deepcopy(candidate)
        changed["parent"]["config"]["approved-edit"] = True
        changed["base_parent"]["content_digest"] = "sha256:" + "f" * 64
        assert compile_authoring_candidate(changed).candidate_digest != approval["candidate_digest"]
        with pytest.raises(
            AuthoringBundleError,
            match="approval is stale or does not match candidate: candidate_digest",
        ):
            publish_authoring_candidate(
                changed, writer, idempotency_key="promote-1", approval=approval
            )
        assert writer.calls == []

        published = publish_authoring_candidate(
            candidate, writer, idempotency_key="promote-1", approval=approval
        )
        assert published["approval"] == approval
        assert published["approval"]["approval_id"] == "approval-1"
        assert published["candidate_digest"] == approval["candidate_digest"]
        assert "approval" not in writer.calls[0][2]
        assert len(writer.calls) == 1
    finally:
        service.close()


def test_frozen_render_restart_then_approval_promotion_is_single_generation(tmp_path):
    """Exercise the contract, durable replay, and candidate promotion seam together."""
    from astrid.core.execution.reconciler import ExecutionReconciler
    from astrid.core.execution.target_adapter import LocalMachineTargetAdapter

    source = b"deterministically extracted source bytes"
    source_digest = "sha256:" + hashlib.sha256(source).hexdigest()
    contract = {
        "schema_version": 1,
        "workflow": {
            "id": "render.authoring-candidate",
            "contract_digest": "sha256:workflow",
            "required_bindings": ["source_video"],
        },
        "inputs": [
            {
                "name": "source_video",
                "object_id": source_digest,
                "filename": "source.mp4",
                "required": True,
            }
        ],
        "target": {"kind": "machine", "id": "local-1"},
        "retry_policy": {"max_attempts": 2},
        "checks": {"outputs": ["sha256"]},
    }

    class Runtime:
        def __init__(self):
            self.task = None
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(("create", kwargs))
            if self.task is None:
                self.task = {
                    "task_id": "render-task-1",
                    "run_id": "render-run-1",
                    "state": "queued",
                    "idempotency_key": kwargs["idempotency_key"],
                    "capability_id": kwargs["capability"],
                    "spec": {**kwargs["spec"], "execution_request": kwargs["execution_request"]},
                }
            return {"task_id": self.task["task_id"], "run_id": self.task["run_id"]}

        def get_task(self, task_id):
            assert task_id == self.task["task_id"]
            return dict(self.task)

        def claim(self, **kwargs):
            self.calls.append(("claim", kwargs))
            self.task.update({"state": "running", "attempt_id": "render-attempt-1"})
            return {
                "task_id": self.task["task_id"],
                "attempt_id": "render-attempt-1",
                "lease_id": "render-lease-1",
                "fence": 1,
                "runtime_epoch": 1,
                "idempotency_key": "render-request-1",
                "capability_id": "render.authoring-candidate",
                "spec": self.task["spec"],
            }

        def settle(self, attempt_id, **kwargs):
            self.calls.append(("settle", kwargs))
            self.task.update({"state": "completed", "result": {"outputs": kwargs["outputs"]}})
            return {"ok": True}

        def fail(self, *args, **kwargs):
            self.calls.append(("fail", kwargs))
            self.task["state"] = "failed"
            return {"ok": True}

    runtime = Runtime()
    adapter = LocalMachineTargetAdapter(
        {"kind": "machine", "id": "local-1"},
        observer=lambda: {
            "kind": "machine",
            "target_id": "local-1",
            "live": True,
            "runtime_epoch": 1,
            "launch_generation": "launch-1",
            "process_birth_id": "process-1",
            "engine_birth_id": "engine-1",
            "output_root": str(tmp_path),
        },
    )
    render_count = 0

    def execute(_claim, observation):
        nonlocal render_count
        render_count += 1
        output = tmp_path / "render.bin"
        output.write_bytes(source + b"/rendered")
        data = output.read_bytes()
        return {
            "outputs": [
                {
                    "path": output.name,
                    "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            ],
            "source_digest": source_digest,
            "source_filename": "source.mp4",
            "output_root": observation.output_root,
        }

    reconciler = ExecutionReconciler(runtime, adapter)
    first = reconciler.run(
        contract=contract,
        project_id="project-1",
        capability="render.authoring-candidate",
        spec={"inputs": {}},
        executor_id="worker-1",
        execute=execute,
        idempotency_key="render-request-1",
    )
    second = reconciler.run(
        contract=contract,
        project_id="project-1",
        capability="render.authoring-candidate",
        spec={"inputs": {}},
        executor_id="worker-1",
        execute=execute,
        idempotency_key="render-request-1",
    )

    assert first.status == "complete"
    assert second.status == "complete"
    assert second.phase == "recovery"
    assert render_count == 1
    assert [name for name, _ in runtime.calls].count("claim") == 1

    parent, shots, timelines = _closure(shared=True)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    approval = approve_authoring_candidate(candidate, approver="editor-1", approval_id="approval-render-1")
    service, candidate, writer = _real_authoring_candidate(tmp_path)
    try:
        approval = approve_authoring_candidate(
            candidate, approver="editor-1", approval_id="approval-render-1"
        )
        promoted = publish_authoring_candidate(
            candidate, writer, idempotency_key="promote-render-1", approval=approval
        )
        assert promoted["approval"] == approval
        assert promoted["candidate_digest"] == approval["candidate_digest"]
        assert len(writer.calls) == 1
    finally:
        service.close()


def test_diff_of_an_unedited_checkout_is_empty_and_names_clips_by_id():
    """Placements were compared dict-vs-list, so every check reported them all as changed."""
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    assert diff_authoring_candidate(candidate)["change_count"] == 0
    shot = next(iter(candidate["shots"].values()))
    clip = shot["internal_timeline"]["clips"][0]
    clip["at"] = clip.get("at", 0) + 0.5
    changed = diff_authoring_candidate(candidate)["changed"]
    assert [row["path"].rsplit(".", 2)[-2:] for row in changed] == [[clip["id"], "at"]]
    candidate["placements"].reverse()
    paths = [row["path"] for row in diff_authoring_candidate(candidate)["changed"]]
    assert ("placements.order" in paths) is (len(candidate["placements"]) > 1)


def test_diff_ignores_float_noise_but_reports_real_clip_edits():
    parent, shots, timelines = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    shot = next(iter(candidate["shots"].values()))
    base = copy.deepcopy(shot["base_internal_timeline"])
    base["clips"][0]["at"] = 2.833333
    shot["base_internal_timeline"] = base
    clip = shot["internal_timeline"]["clips"][0]
    clip["at"] = 2.8333330000000005
    assert diff_authoring_candidate(candidate)["change_count"] == 0

    clip["at"] = 2.8345
    changed = diff_authoring_candidate(candidate)["changed"]
    assert [row["path"].rsplit(".", 2)[-2:] for row in changed] == [[clip["id"], "at"]]


# D72 migration of upstream-924 selector 11522.  The detached approval remains
# bound to the candidate, while publication uses the authenticated commit port.
def test_d66_upstream_approval_is_bound_to_candidate_and_base_revision(tmp_path):
    service, candidate, writer = _real_authoring_candidate(tmp_path)
    try:
        approval = approve_authoring_candidate(
            candidate, approver="editor-1", approval_id="approval-1"
        )
        assert approval["base_parent"] == candidate["base_parent"]

        changed = copy.deepcopy(candidate)
        changed["parent"]["config"]["approved-edit"] = True
        changed["base_parent"]["content_digest"] = "sha256:" + "f" * 64
        assert compile_authoring_candidate(changed).candidate_digest != approval["candidate_digest"]
        with pytest.raises(
            AuthoringBundleError,
            match="approval is stale or does not match candidate: candidate_digest",
        ):
            publish_authoring_candidate(
                changed, writer, idempotency_key="promote-1", approval=approval
            )
        assert writer.calls == []

        published = publish_authoring_candidate(
            candidate, writer, idempotency_key="promote-1", approval=approval
        )
        assert published["approval"] == approval
        assert published["approval"]["approval_id"] == "approval-1"
        assert published["candidate_digest"] == approval["candidate_digest"]
        assert "approval" not in writer.calls[0][2]
        assert len(writer.calls) == 1
    finally:
        service.close()


# D72 migration of upstream-924 selector 11523.  Render/restart assertions are
# retained; the final publication uses the authenticated commit port.
def test_d66_upstream_frozen_render_restart_then_approval_promotion_is_single_generation(tmp_path):
    """Exercise the contract, durable replay, and candidate promotion seam together."""
    from astrid.core.execution.reconciler import ExecutionReconciler
    from astrid.core.execution.target_adapter import LocalMachineTargetAdapter

    source = b"deterministically extracted source bytes"
    source_digest = "sha256:" + hashlib.sha256(source).hexdigest()
    contract = {
        "schema_version": 1,
        "workflow": {
            "id": "render.authoring-candidate",
            "contract_digest": "sha256:workflow",
            "required_bindings": ["source_video"],
        },
        "inputs": [
            {
                "name": "source_video",
                "object_id": source_digest,
                "filename": "source.mp4",
                "required": True,
            }
        ],
        "target": {"kind": "machine", "id": "local-1"},
        "retry_policy": {"max_attempts": 2},
        "checks": {"outputs": ["sha256"]},
    }

    class Runtime:
        def __init__(self):
            self.task = None
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(("create", kwargs))
            if self.task is None:
                self.task = {
                    "task_id": "render-task-1",
                    "run_id": "render-run-1",
                    "state": "queued",
                    "idempotency_key": kwargs["idempotency_key"],
                    "capability_id": kwargs["capability"],
                    "spec": {**kwargs["spec"], "execution_request": kwargs["execution_request"]},
                }
            return {"task_id": self.task["task_id"], "run_id": self.task["run_id"]}

        def get_task(self, task_id):
            assert task_id == self.task["task_id"]
            return dict(self.task)

        def claim(self, **kwargs):
            self.calls.append(("claim", kwargs))
            self.task.update({"state": "running", "attempt_id": "render-attempt-1"})
            return {
                "task_id": self.task["task_id"],
                "attempt_id": "render-attempt-1",
                "lease_id": "render-lease-1",
                "fence": 1,
                "runtime_epoch": 1,
                "idempotency_key": "render-request-1",
                "capability_id": "render.authoring-candidate",
                "spec": self.task["spec"],
            }

        def settle(self, attempt_id, **kwargs):
            self.calls.append(("settle", kwargs))
            self.task.update({"state": "completed", "result": {"outputs": kwargs["outputs"]}})
            return {"ok": True}

        def fail(self, *args, **kwargs):
            self.calls.append(("fail", kwargs))
            self.task["state"] = "failed"
            return {"ok": True}

    runtime = Runtime()
    adapter = LocalMachineTargetAdapter(
        {"kind": "machine", "id": "local-1"},
        observer=lambda: {
            "kind": "machine",
            "target_id": "local-1",
            "live": True,
            "runtime_epoch": 1,
            "launch_generation": "launch-1",
            "process_birth_id": "process-1",
            "engine_birth_id": "engine-1",
            "output_root": str(tmp_path),
        },
    )
    render_count = 0

    def execute(_claim, observation):
        nonlocal render_count
        render_count += 1
        output = tmp_path / "render.bin"
        output.write_bytes(source + b"/rendered")
        data = output.read_bytes()
        return {
            "outputs": [
                {
                    "path": output.name,
                    "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            ],
            "source_digest": source_digest,
            "source_filename": "source.mp4",
            "output_root": observation.output_root,
        }

    reconciler = ExecutionReconciler(runtime, adapter)
    first = reconciler.run(
        contract=contract,
        project_id="project-1",
        capability="render.authoring-candidate",
        spec={"inputs": {}},
        executor_id="worker-1",
        execute=execute,
        idempotency_key="render-request-1",
    )
    second = reconciler.run(
        contract=contract,
        project_id="project-1",
        capability="render.authoring-candidate",
        spec={"inputs": {}},
        executor_id="worker-1",
        execute=execute,
        idempotency_key="render-request-1",
    )

    assert first.status == "complete"
    assert second.status == "complete"
    assert second.phase == "recovery"
    assert render_count == 1
    assert [name for name, _ in runtime.calls].count("claim") == 1

    service, candidate, writer = _real_authoring_candidate(tmp_path)
    try:
        approval = approve_authoring_candidate(
            candidate, approver="editor-1", approval_id="approval-render-1"
        )
        published = publish_authoring_candidate(
            candidate, writer, idempotency_key="promote-render-1", approval=approval
        )
        assert published["approval"] == approval
        assert published["candidate_digest"] == approval["candidate_digest"]
        assert len(writer.calls) == 1
    finally:
        service.close()


# Parent coverage retained during the Release-1 + Timeline merge.
def test_approval_is_bound_to_candidate_and_base_revision():
    from astrid.core.timeline.authoring_bundle import promote_approved_authoring_candidate

    parent, shots, timelines = _closure(shared=True)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=timelines
    )
    approval = approve_authoring_candidate(
        candidate, approver="editor-1", approval_id="approval-1"
    )
    writer = _Writer()

    changed = copy.deepcopy(candidate)
    changed["parent"]["config"]["approved-edit"] = True
    with pytest.raises(AuthoringBundleError, match="stale or does not match"):
        promote_approved_authoring_candidate(
            changed, approval, writer, idempotency_key="promote-1"
        )
    assert writer.calls == []

    promoted = promote_approved_authoring_candidate(
        candidate, approval, writer, idempotency_key="promote-1"
    )
    assert promoted["approval"]["approval_id"] == "approval-1"
    assert len(writer.calls) == 1
