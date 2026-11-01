from __future__ import annotations

import copy
import os
import subprocess
import sys

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline.commit import (
    _digest,
    _json,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)

MEDIA = "sha256:" + "1" * 64


def document_fixture(*, shared=True):
    clip = {"id": "clip-a", "track": "picture", "asset": "picture", "clipType": "media",
            "at": 1, "hold": 2, "app": {"org.example": {"literal": "clip-a", "clear": None}}}
    config = {"tracks": [{"id": "picture", "kind": "visual"}],
              "clips": [clip, dict(copy.deepcopy(clip), id="clip-b", at=5)],
              "output": {"fps": {"numerator": 30000, "denominator": 1001}},
              "opaque": {"timestamp": "authored timestamp", "v": [None, False, 0]}}
    registry = {"assets": {"picture": {"media_id": MEDIA, "type": "image"}}}
    internal = {"revision_id": "internal-1", "timeline_id": "chapter-1",
                "payload": {**copy.deepcopy(config), "registry": registry,
                            "source_window": {"start": 4, "end": 9}, "unknown": [1, 2]}}
    shot = {"shot_id": "chapter-shot", "revision_id": "shot-1",
            "internal_timeline_revision_id": "internal-1",
            "payload": {"name": "Chapter One", "internal_timeline_revision_id": "internal-1",
                        "items": [{"item_id": "item-1", "media_id": MEDIA,
                                   "metadata": {"opaque_literal": "item-1"}}],
                        "text_bindings": [{"slot": "narration", "text": "A chapter"}],
                        "metadata": {"chapter": True}, "unknown": {"keep": None}}}
    occurrence = {"occurrence_id": "occ-a", "shot_id": "chapter-shot",
                  "shot_revision_id": "shot-1", "placement": {"start_ms": 0, "track": "picture"},
                  "duration_ms": 10000, "source_offset": {"start": 0, "end": 10000},
                  "speed": {"numerator": 1, "denominator": 1}, "track": "picture",
                  "unknown": {"literal": "chapter-shot"}}
    occurrences = [occurrence]
    if shared:
        second = copy.deepcopy(occurrence)
        second.update(occurrence_id="occ-b", placement={"start_ms": 10000, "track": "picture"})
        occurrences.append(second)
    parent = {"revision_id": "parent-1", "payload": {
        "config": copy.deepcopy(config), "clips": copy.deepcopy(config["clips"]),
        "registry": registry, "occurrences": occurrences,
        "name": "Renamed Film", "unknown": {"opaque": ["parent-1", None]},
    }}
    for image in (parent, shot, internal):
        image["content_digest"] = _digest(image["payload"])
    return {"schema_version": 1, "project_id": "project-1", "timeline_id": "film",
            "parent_revision": parent, "shot_revisions": [shot],
            "internal_timeline_revisions": [internal],
            "dependency_manifest": _manifest([shot], [internal], [
                {"media_id": MEDIA, "content_digest": MEDIA}], [])}


@pytest.mark.parametrize("shared", [False, True])
def test_complete_document_roundtrip_reuses_original_images_and_unknown_fields(shared):
    document = document_fixture(shared=shared)
    original = copy.deepcopy(document)
    bundle = bundle_from_document(document)
    assert document_from_bundle(bundle) == original
    assert document == original
    bundle["parent"]["unknown"]["opaque"].append("detached")
    assert document == original


def test_canonical_reload_filters_authored_meta_but_discovers_component_params_media():
    document = document_fixture(shared=False)
    authored_only = "sha256:" + "2" * 64
    component_media = "sha256:" + "3" * 64
    internal = document["internal_timeline_revisions"][0]
    clip = internal["payload"]["clips"][0]
    clip["meta"] = {"org.example.notes": {"media_id": authored_only}}
    clip["params"] = {"meta": {"media_id": component_media}}
    internal["content_digest"] = _digest(internal["payload"])
    document["dependency_manifest"]["media"] = [
        {"media_id": MEDIA, "content_digest": MEDIA},
        {"media_id": component_media, "content_digest": component_media},
    ]
    document["dependency_manifest"] = _manifest(
        document["shot_revisions"], document["internal_timeline_revisions"],
        document["dependency_manifest"]["media"], [],
    )

    assert document_from_bundle(bundle_from_document(document)) == document

    missing_component_media = copy.deepcopy(document)
    missing_component_media["dependency_manifest"]["media"] = [
        {"media_id": MEDIA, "content_digest": MEDIA},
    ]
    with pytest.raises(AuthoringBundleError, match="missing media dependency"):
        bundle_from_document(missing_component_media)


def test_parent_edit_keeps_shared_child_closure_and_main_fields():
    document = document_fixture()
    bundle = bundle_from_document(document)
    bundle["parent"]["config"]["clips"][0]["at"] = 2
    bundle["parent"]["clips"][0]["at"] = 2
    candidate = document_from_bundle(bundle)
    assert candidate["shot_revisions"] == document["shot_revisions"]
    assert candidate["internal_timeline_revisions"] == document["internal_timeline_revisions"]
    assert candidate["dependency_manifest"] == document["dependency_manifest"]
    parent = candidate["parent_revision"]["payload"]
    assert parent["occurrences"] == document["parent_revision"]["payload"]["occurrences"]
    assert parent["name"] == "Renamed Film"
    assert parent["unknown"] == document["parent_revision"]["payload"]["unknown"]
    assert parent["config"]["output"]["fps"] == {"numerator": 30000, "denominator": 1001}
    assert candidate["parent_revision"]["revision_id"] != "parent-1"


def test_occurrence_edit_closes_reused_sibling_and_new_exact_child():
    document = document_fixture()
    bundle = bundle_from_document(document)
    shot = bundle["shots"][bundle["placements"][0]["shot_id"]]
    shot["internal_timeline"]["clips"][0]["at"] = 3
    candidate = document_from_bundle(bundle)
    assert len(candidate["shot_revisions"]) == 2
    assert len(candidate["internal_timeline_revisions"]) == 2
    assert document["shot_revisions"][0] in candidate["shot_revisions"]
    assert document["internal_timeline_revisions"][0] in candidate["internal_timeline_revisions"]
    changed = next(row for row in candidate["internal_timeline_revisions"]
                   if row["revision_id"] != "internal-1")
    assert changed["payload"]["clips"][0]["at"] == 3
    assert changed["payload"]["source_window"] == {"start": 4, "end": 9}
    assert changed["payload"]["unknown"] == [1, 2]
    assert document_from_bundle(bundle) == candidate


@pytest.mark.parametrize("damage", ["digest", "missing_child", "missing_internal", "foreign_project",
                                   "foreign_image", "shot_link", "manifest", "missing_media",
                                   "extra_child", "duplicate_child", "nonfinite", "bad_pin"])
def test_malformed_complete_closure_fails_closed(damage):
    document = document_fixture()
    if damage == "digest":
        document["shot_revisions"][0]["payload"]["name"] = "corrupt"
    elif damage == "missing_child":
        document["shot_revisions"] = []
    elif damage == "missing_internal":
        document["internal_timeline_revisions"] = []
    elif damage == "foreign_project":
        document["project_id"] = ""
    elif damage == "foreign_image":
        document["shot_revisions"][0]["project_id"] = "other-project"
    elif damage == "shot_link":
        document["shot_revisions"][0]["internal_timeline_revision_id"] = "other-revision"
    elif damage == "manifest":
        document["dependency_manifest"]["shots"][0]["revision_id"] = "latest-child"
    elif damage == "missing_media":
        document["dependency_manifest"]["media"] = []
    elif damage == "extra_child":
        document["shot_revisions"].append(dict(copy.deepcopy(document["shot_revisions"][0]),
                                                shot_id="orphan", revision_id="orphan-1"))
    elif damage == "duplicate_child":
        document["shot_revisions"].append(copy.deepcopy(document["shot_revisions"][0]))
    elif damage == "nonfinite":
        document["parent_revision"]["payload"]["unknown"] = float("inf")
    elif damage == "bad_pin":
        document["dependency_manifest"]["documents"] = [{"document_id": "x", "version": True,
                                                         "content_digest": MEDIA}]
    with pytest.raises((AuthoringBundleError, ValueError)):
        bundle_from_document(document)


def test_retained_document_pins_are_part_of_revision_identity():
    document = document_fixture()
    bundle = bundle_from_document(document)
    pin = {"document_id": "timeline.config", "version": 1, "content_digest": MEDIA}
    candidate = document_from_bundle(bundle, document_pins=[pin])
    assert candidate["parent_revision"]["payload"] == document["parent_revision"]["payload"]
    assert candidate["parent_revision"]["revision_id"] != document["parent_revision"]["revision_id"]
    assert candidate["dependency_manifest"]["documents"] == [pin]
    assert document_from_bundle(bundle_from_document(candidate)) == candidate


@pytest.mark.parametrize('before,after', [(2, 2.0), (1, True), (True, 1)])
@pytest.mark.parametrize('scope', ['parent', 'shot', 'internal'])
def test_immutable_image_reuse_requires_exact_encoded_authored_content(scope, before, after):
    original = document_fixture(shared=False)
    image = {'parent': original['parent_revision'], 'shot': original['shot_revisions'][0],
             'internal': original['internal_timeline_revisions'][0]}[scope]
    image['payload']['unknown'] = {'x': before, 'opaque': [None, False, 1, 1.0]}
    image['content_digest'] = _digest(image['payload'])
    original['dependency_manifest'] = _manifest(
        original['shot_revisions'], original['internal_timeline_revisions'],
        original['dependency_manifest']['media'], [])
    source = bundle_from_document(original)
    candidate = copy.deepcopy(source)
    shot = next(iter(candidate['shots'].values()))
    target = {'parent': candidate['parent'], 'shot': shot['payload'],
              'internal': shot['internal_timeline']}[scope]
    target['unknown']['x'] = after
    # The authenticated historical image must not override an exact new value.
    result = document_from_bundle(candidate, source_bundles=(source,))
    changed = {'parent': result['parent_revision'], 'shot': result['shot_revisions'][0],
               'internal': result['internal_timeline_revisions'][0]}[scope]
    assert type(changed['payload']['unknown']['x']) is type(after)
    assert changed['payload']['unknown']['opaque'] == [None, False, 1, 1.0]
    assert changed['revision_id'] != image['revision_id']
    assert changed['content_digest'] == _digest(changed['payload'])
    assert changed['content_digest'] != image['content_digest']
    assert _json(document_from_bundle(bundle_from_document(result))) == _json(result)
    assert _json(source['source_mapping']['_runtime_document']) == _json(original)


def test_pure_converter_imports_without_runtime_or_renderer():
    script = r'''
import sys
class Blocked:
    def find_spec(self, fullname, *args):
        if (fullname.startswith(('runtime_protocol', 'astrid.packs.rendering',
                                 'astrid.core.rendering', 'astrid.sdk.authoring_render_preview',
                                 'astrid.core.timeline.compatibility', 'composition'))
                or 'scene' in fullname):
            raise AssertionError('forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Blocked())
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
assert callable(bundle_from_document) and callable(document_from_bundle)
from tests.sdk.test_timeline_commit_provider_document import document_fixture
document = document_fixture()
assert document_from_bundle(bundle_from_document(document)) == document
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
