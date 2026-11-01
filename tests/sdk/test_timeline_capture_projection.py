"""Pure capture projection on the existing semantic provider; no save dependency."""
import builtins
import copy
import io
import json
import socket
import subprocess
from pathlib import Path

import pytest

from astrid.core.timeline.authoring_bundle import (
    AuthoringBundleError,
    open_authoring_bundle,
    preview_authoring_candidate,
    publish_authoring_candidate,
)
from astrid.sdk.timeline.commit import RuntimeCommitService, bundle_from_document
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def project(document, preview=None):
    return RuntimeCommitService("unused-by-pure-capture").project_capture(
        document, authoring_preview=preview)


def draft(document):
    bundle = bundle_from_document(document)
    bundle["parent"]["config"]["opaque_draft"] = [None, False, 0, 2.0]
    first = next(iter(bundle["shots"].values()))
    first["internal_timeline"]["clips"][0]["at"] = 2.002
    return bundle


@pytest.mark.parametrize("changed", [False, True])
def test_capture_candidate_inputs_exact_pure_detached_golden(monkeypatch, changed):
    document = document_fixture()
    candidate = draft(document) if changed else bundle_from_document(document)
    before = copy.deepcopy((document, candidate))
    expected = {"base": bundle_from_document(document),
                "preview": preview_authoring_candidate(candidate)}
    provider = RuntimeCommitService("unused-by-pure-capture")

    def forbidden(*args, **kwargs):
        raise AssertionError("pure capture inputs attempted IO")

    with monkeypatch.context() as no_io:
        for target, name in ((builtins, "open"), (io, "open"), (Path, "open"),
                             (socket, "socket"), (socket, "create_connection"),
                             (subprocess, "Popen"), (subprocess, "run")):
            no_io.setattr(target, name, forbidden)
        result = provider.capture_candidate_inputs(document, candidate=candidate)
        assert json.dumps(result, sort_keys=True, allow_nan=False) == json.dumps(
            expected, sort_keys=True, allow_nan=False)
        result["base"]["parent"]["config"]["opaque"]["v"].append("caller change")
        result["preview"]["candidate"]["parent"]["config"]["opaque"]["v"].append("caller change")
        assert provider.capture_candidate_inputs(document, candidate=candidate) == expected
    assert (document, candidate) == before


@pytest.mark.parametrize("candidate", [False, True])
def test_projection_is_detached_json_deterministic_without_io(monkeypatch, candidate):
    document = document_fixture()
    preview = preview_authoring_candidate(draft(document)) if candidate else None
    before = copy.deepcopy((document, preview))
    expected = project(document, preview)

    def forbidden(*args, **kwargs):
        raise AssertionError("pure capture projection attempted IO")

    import astrid.core.timeline.shot_composition_projection as projector
    original = projector.project_runtime_parent_composition
    calls = []

    def one_projector(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(projector, "project_runtime_parent_composition", one_projector)
    with monkeypatch.context() as no_io:
        for target, name in ((builtins, "open"), (io, "open"), (Path, "open"),
                             (socket, "socket"), (socket, "create_connection"),
                             (subprocess, "Popen"), (subprocess, "run")):
            no_io.setattr(target, name, forbidden)
        for _ in range(3):
            result = project(copy.deepcopy(document), copy.deepcopy(preview))
            assert json.dumps(result, sort_keys=True, allow_nan=False) == json.dumps(
                expected, sort_keys=True, allow_nan=False)
            result["config"]["opaque"]["v"].append("caller change")
    assert len(calls) == 3
    assert (document, preview) == before
    assert expected["config"]["output"]["fps"] == {"numerator": 30000, "denominator": 1001}
    assert expected["config"]["clips"][0]["app"]["org.example"]["clear"] is None
    assert set(expected) == {"config", "registry", "expansion"}
    if candidate:
        marker = expected["expansion"]["working_copy"]
        assert marker["base_parent"] == preview["head"]
        assert marker["candidate_digest"] == preview["candidate_digest"]
        assert marker["publication_digest"] == preview["publication_digest"]
        assert marker["candidate_parent_revision_id"] != document["parent_revision"]["revision_id"]


@pytest.mark.parametrize("damage", ["parent_digest", "shot_digest", "internal_digest", "missing_shot",
                                    "missing_internal", "orphan", "link", "scope", "manifest"])
def test_inconsistent_saved_closure_refuses(damage):
    document = document_fixture()
    if damage.endswith("_digest"):
        family = {"parent_digest": "parent_revision", "shot_digest": "shot_revisions",
                  "internal_digest": "internal_timeline_revisions"}[damage]
        row = document[family] if family == "parent_revision" else document[family][0]
        row["content_digest"] = "sha256:" + "0" * 64
    elif damage == "missing_shot":
        document["shot_revisions"] = []
    elif damage == "missing_internal":
        document["internal_timeline_revisions"] = []
    elif damage == "orphan":
        document["shot_revisions"].append(copy.deepcopy(document["shot_revisions"][0]))
    elif damage == "link":
        document["shot_revisions"][0]["internal_timeline_revision_id"] = "foreign"
    elif damage == "scope":
        document["shot_revisions"][0]["project_id"] = "foreign"
    else:
        document["dependency_manifest"]["shots"][0]["content_digest"] = "sha256:" + "0" * 64
    with pytest.raises(AuthoringBundleError):
        project(document)


@pytest.mark.parametrize("damage", ["kind", "head", "candidate_digest", "publication_digest", "publication",
                                    "project_id", "timeline_id", "base_parent", "base_parent_payload",
                                    "base_placements", "source", "child_base", "internal_base", "embedded_base",
                                    "retained_pin", "missing_candidate", "candidate_after_freeze"])
def test_inconsistent_candidate_refuses_even_with_recomputed_preview(damage):
    document = document_fixture()
    bundle = draft(document)
    identity = next(iter(bundle["shots"]))
    if damage in {"project_id", "timeline_id"}:
        bundle[damage] = "foreign"
    elif damage == "base_parent":
        bundle[damage]["revision_id"] = "other-parent"
    elif damage in {"base_parent_payload", "base_placements"}:
        bundle[damage]["forged"] = True
    elif damage == "source":
        bundle["source_mapping"]["placements"] = {}
    elif damage in {"child_base", "internal_base"}:
        key = "base_payload" if damage == "child_base" else "base_internal_timeline"
        bundle["shots"][identity][key]["forged"] = True
    elif damage == "embedded_base":
        bundle["source_mapping"]["_runtime_document"]["project_id"] = "foreign"
    elif damage == "retained_pin":
        bundle["source_mapping"]["shots"][identity]["internal_timeline_content_digest"] = "sha256:" + "0" * 64
    preview = preview_authoring_candidate(bundle)
    if damage in {"kind", "candidate_digest", "publication_digest", "head", "publication"}:
        preview[damage] = {} if damage in {"head", "publication"} else "forged"
    elif damage == "missing_candidate":
        preview.pop("candidate")
    elif damage == "candidate_after_freeze":
        preview["candidate"]["parent"]["config"]["opaque_draft"] = "changed after freezing"
    with pytest.raises(AuthoringBundleError):
        project(document, preview)


@pytest.mark.parametrize("value", [True, 2.0])
def test_json_distinct_base_values_are_not_equal(value):
    document = document_fixture()
    preview = preview_authoring_candidate(draft(document))
    # JSON bool/int and float/int comparisons must preserve encoded identity.
    preview["candidate"]["base_parent_payload"]["config"]["clips"][0]["hold"] = value
    with pytest.raises(AuthoringBundleError, match="exact base"):
        project(document, preview)


def test_reused_candidate_pin_is_compared_with_verified_image():
    document = document_fixture(shared=False)
    bundle = bundle_from_document(document)
    bundle["source_mapping"]["shots"]["chapter-shot"]["content_digest"] = "sha256:" + "0" * 64
    preview = preview_authoring_candidate(bundle)
    with pytest.raises(AuthoringBundleError, match="child source"):
        project(document, preview)


def test_capture_method_imports_and_runs_without_renderer_or_runtime():
    import os
    import sys

    script = r'''
import sys
class Blocked:
    def find_spec(self, fullname, *args):
        if fullname.startswith(('runtime_protocol', 'astrid.packs.rendering',
                                'astrid.core.rendering', 'astrid.sdk.authoring_render_preview',
                                'astrid.core.timeline.compatibility', 'composition')) or 'scene' in fullname:
            raise AssertionError('forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Blocked())
from tests.sdk.test_timeline_capture_projection import project, draft
from tests.sdk.test_timeline_commit_provider_document import document_fixture
from astrid.core.timeline.authoring_bundle import preview_authoring_candidate
document = document_fixture()
assert project(document)['expansion']['canonical'] is True
assert project(document, preview_authoring_candidate(draft(document)))['expansion']['working_copy']
from astrid.sdk.timeline.commit import RuntimeCommitService
assert set(RuntimeCommitService('pure').capture_candidate_inputs(document, candidate=draft(document))) == {'base', 'preview'}
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr


@pytest.fixture
def saved_runtime(tmp_path):
    from tests.sdk import test_capture_source_runtime as captures

    yield from captures.open_source_runtime(tmp_path)


def _saved_document(runtime):
    parent = runtime.get_project_parent_composition_revision(
        runtime.project_id, runtime.timeline_id, runtime.parent_revision_id,
    )
    shots, internals, seen_shots, seen_internals = [], [], set(), set()
    for occurrence in parent["payload"]["occurrences"]:
        shot_key = (occurrence["shot_id"], occurrence["shot_revision_id"])
        if shot_key not in seen_shots:
            seen_shots.add(shot_key)
            shot = runtime.get_project_shot_revision(runtime.project_id, *shot_key)
            shots.append(shot)
        internal_id = next(row for row in shots if (row["shot_id"], row["revision_id"]) == shot_key)[
            "internal_timeline_revision_id"
        ]
        if internal_id not in seen_internals:
            seen_internals.add(internal_id)
            internals.append(runtime.get_project_timeline_revision(
                runtime.project_id, runtime.timeline_id, internal_id,
            ))
    return {
        "schema_version": 1,
        "project_id": runtime.project_id,
        "timeline_id": runtime.timeline_id,
        "parent_revision": {key: parent[key] for key in ("revision_id", "content_digest", "payload")},
        "shot_revisions": shots,
        "internal_timeline_revisions": internals,
        "dependency_manifest": parent["dependency_manifest"],
    }


class _RuntimeAuthoringWriter:
    """Expose the authenticated Runtime commit port to authoring publication."""

    def __init__(self, service):
        self.service = service

    def list_timeline_commits(self, project_id, timeline_id, **kwargs):
        return self.service.list_timeline_commits(
            project_id, timeline_id, authorize=lambda: None, **kwargs,
        )

    def get_project_media_selection(self, project_id, selection):
        return self.service.get_project_media_selection(
            project_id, selection, authorize=lambda: None,
        )

    def __getattr__(self, name):
        return getattr(self.service, name)

    def commit_timeline(self, project_id, timeline_id, body, *, idempotency_key):
        return self.service.commit_timeline(
            project_id, timeline_id, body, idempotency_key=idempotency_key,
            publisher="capture-projection-fixture",
            invocation_path=f"/v1/projects/{project_id}/timelines/{timeline_id}/commit",
            authorize=lambda: None,
        )


@pytest.fixture
def saved_closure(saved_runtime):
    """Public publication with reference order different from canonical sort order."""
    from astrid.sdk.timeline_editing import duplicate_authoring_shot, remove_authoring_shot

    runtime = saved_runtime
    document = _saved_document(runtime)
    parent = document["parent_revision"]
    bundle = open_authoring_bundle(
        {**parent, "project_id": runtime.project_id, "timeline_id": runtime.timeline_id},
        shot_revisions=[{**row, "project_id": runtime.project_id} for row in document["shot_revisions"]],
        internal_timeline_revisions=[{**row, "project_id": runtime.project_id}
                                     for row in document["internal_timeline_revisions"]],
    )
    source_shot = next(iter(bundle["shots"]))
    source = bundle["shots"][source_shot]
    source_payload = source["payload"]
    source_internal = source["internal_timeline"]
    source_media_id = source_internal["registry"]["assets"][
        source_internal["clips"][0]["asset"]
    ]["media_id"]

    # Keep the original capture baseline's opaque and authored extension fields
    # in the candidate.  The publication below persists them through the
    # registered current commit provider, so this remains a real source graph,
    # not a fixture-only default or a repaired direct lookup.
    bundle["parent"]["config"]["opaque_parent_config"] = {"keep": True}
    bundle["parent"]["opaque_parent"] = {"keep": ["all", "fields"]}
    bundle["parent"].setdefault("registry", {})["opaque_registry"] = {"keep": [1, 2, 3]}
    occurrence = bundle["placements"][0]
    occurrence["placement"]["opaque_geometry"] = "keep"
    occurrence["transform"] = {"scale": 1, "opaque_transform": [4, 5]}
    occurrence["muted"] = False
    occurrence["provenance"] = {"placement": "first"}
    occurrence["future_occurrence"] = {"round_trip": True}

    source_payload["metadata"] = {"settings": {"look": "warm"}, "opaque": ["keep"]}
    source_payload["opaque_shot"] = {"future_schema": 9}
    source_payload["pools"] = [{"id": "pool-1", "opaque": "keep"}]
    source_payload["selected_variants"] = {"pool-1": "variant-1"}
    source_payload["provenance"] = {"source": "fixture", "shot_id_literal": source_shot}
    source_payload["generation_inputs"] = {"visual": {"object_id": source_media_id}}
    source_payload["text_bindings"] = [{"slot": "voiceover", "text": "hello"}]
    item = source_payload["items"][0]
    item["source_frame"] = None
    item["metadata"].update({
        "role": "primary_visual",
        "source_item_id": item["item_id"],
        "opaque_literal": "item-1",
    })

    source_internal["effects"] = [{"type": "opacity", "value": 0.75}]
    source_internal["audio"] = [{"track_id": "voice", "gain": 0.8}]
    source_internal["layout"] = {"width": 1920, "height": 1080}
    source_internal["assets"] = []
    source_internal["opaque_timeline"] = {"future_schema": 7}
    source_internal["registry"]["opaque_registry"] = {"keep": [1, 2, 3]}
    source_internal["tracks"][0]["opaque_track"] = {"future": True}
    source_internal["clips"][0]["opaque_clip"] = {"literal": "item-1"}

    # The current duplicate API copies the explicit authoring baselines.  The
    # source is removed below, so author its complete current image before the
    # duplicate commands and retain the original source mapping separately.
    source["base_payload"] = copy.deepcopy(source_payload)
    source["base_internal_timeline"] = copy.deepcopy(source_internal)

    duplicate_authoring_shot(bundle, source_shot, new_shot_id="shot-z", occurrence_id="occ-z", start_ms=0)
    duplicate_authoring_shot(bundle, source_shot, new_shot_id="shot-a", occurrence_id="occ-a", start_ms=1000)
    remove_authoring_shot(bundle, source_shot)
    bundle["parent"]["created_at"] = "authored parent"
    bundle["parent"]["dependency_manifest"] = {"opaque": {"created_at": [None, False, 0]}}
    bundle["parent"]["config"]["theme_overrides"] = {
        "visual": {"canvas": {"fps": [30000, 1001]}},
    }
    for name in ("z", "a"):
        shot = bundle["shots"][f"shot-{name}"]
        internal = shot["internal_timeline"]
        internal["opaque_timeline"]["source"] = name
        internal["created_at"] = f"authored internal {name}"
        internal["dependency_manifest"] = {"opaque": [None, False, 0, {"created_at": name}]}
        shot["payload"]["created_at"] = f"authored shot {name}"
        shot["payload"].setdefault("metadata", {})["dependency_manifest"] = {"opaque": [name, None]}
    writer = _RuntimeAuthoringWriter(runtime.service)
    published = publish_authoring_candidate(bundle, writer, idempotency_key="capture-projection-golden")
    document = published["publication"]["canonical_document"]
    runtime.parent_revision_id = document["parent_revision"]["revision_id"]
    runtime.golden_parent_revision_id = runtime.parent_revision_id
    runtime.golden_internal_revision_ids = {
        name: next(
            row["revision_id"] for row in document["internal_timeline_revisions"]
            if row["payload"].get("opaque_timeline", {}).get("source") == name
        )
        for name in ("z", "a")
    }
    return runtime, document


@pytest.mark.integration
def test_real_saved_projection_config_registry_and_full_pins_golden(saved_closure):
    from astrid.packs.rendering.actions.render.managed_timeline import _project_exact_parent_head

    runtime, document = saved_closure
    parent, expected, expansion = _project_exact_parent_head(
        client=runtime, project_id=runtime.project_id, timeline_id="main",
        parent_revision_id=runtime.golden_parent_revision_id)
    result = project(document)
    assert result["config"] == expected.config
    assert result["registry"] == expected.registry
    assert result["expansion"]["parent_content_digest"] == parent["content_digest"]
    for key in ("canonical", "parent_revision_id", "children", "shots", "occurrences", "outputs"):
        assert result["expansion"][key] == expansion[key]
    assert [row["shot_id"] for row in result["expansion"]["shots"]] == ["shot-z", "shot-a"]
    assert result["expansion"]["shots"][0]["content_digest"] == next(
        row["content_digest"] for row in document["shot_revisions"] if row["shot_id"] == "shot-z")
    assert result["expansion"]["children"][0]["config_hash"] == next(
        row["content_digest"] for row in document["internal_timeline_revisions"]
        if row["revision_id"] == runtime.golden_internal_revision_ids["z"])


@pytest.mark.integration
def test_real_saved_entire_expansion_golden(saved_closure):
    from astrid.packs.rendering.actions.render.managed_timeline import _project_exact_parent_head

    runtime, document = saved_closure
    parent, projected, expansion = _project_exact_parent_head(
        client=runtime, project_id=runtime.project_id, timeline_id="main",
        parent_revision_id=runtime.golden_parent_revision_id)
    assert project(document) == {
        "config": projected.config, "registry": projected.registry,
        "expansion": {**expansion, "parent_content_digest": parent["content_digest"]},
    }


@pytest.mark.integration
def test_saved_canonical_images_preserve_authored_fields_pins_order_and_read_rows(saved_closure, monkeypatch):
    from astrid.packs.rendering.actions.render.managed_timeline import _project_exact_parent_head

    runtime, document = saved_closure
    parent = runtime.get_project_parent_composition_revision(
        runtime.project_id, "main", runtime.golden_parent_revision_id)
    shots = {(row["shot_id"], row["revision_id"]): runtime.get_project_shot_revision(
        runtime.project_id, row["shot_id"], row["revision_id"]) for row in document["shot_revisions"]}
    internals = {row["revision_id"]: runtime.get_project_timeline_revision(
        runtime.project_id, "main", row["revision_id"]) for row in document["internal_timeline_revisions"]}
    # Exercise the duplicated read-envelope manifest, including a retained document pin.
    manifest = copy.deepcopy(document["dependency_manifest"])
    manifest["documents"] = [{"document_id": "retained-transcript", "version": 7,
                              "content_digest": "sha256:" + "1" * 64}]
    parent["dependency_manifest"] = manifest
    before = copy.deepcopy((parent, shots, internals, document))
    monkeypatch.setattr(runtime, "get_project_parent_composition_revision", lambda *args: parent)
    monkeypatch.setattr(runtime, "get_project_shot_revision", lambda p, s, r: shots[s, r])
    monkeypatch.setattr(runtime, "get_project_timeline_revision", lambda p, t, r: internals[r])
    returned, projected, expansion = _project_exact_parent_head(
        client=runtime, project_id=runtime.project_id, timeline_id="main",
        parent_revision_id=runtime.golden_parent_revision_id)
    graph = expansion["graph"]
    assert graph == project(document)["expansion"]["graph"]
    assert returned["dependency_manifest"] == manifest
    assert manifest["shots"] == document["dependency_manifest"]["shots"]
    assert manifest["internal_timelines"] == document["dependency_manifest"]["internal_timelines"]
    assert manifest["media"] == document["dependency_manifest"]["media"]
    assert manifest["media"] and manifest["documents"]
    assert graph["parent_revision"]["content_digest"] == document["parent_revision"]["content_digest"]
    assert "dependency_manifest" not in graph["parent_revision"]
    assert [row["shot_id"] for row in graph["shot_revisions"]] == ["shot-z", "shot-a"]
    assert [row["revision_id"] for row in graph["internal_timeline_revisions"]] == [
        runtime.golden_internal_revision_ids["z"], runtime.golden_internal_revision_ids["a"]]
    images = [graph["parent_revision"], *graph["shot_revisions"], *graph["internal_timeline_revisions"]]
    for row in images:
        assert "created_at" not in row
        assert row["payload"]["created_at"].startswith("authored ")
    assert graph["parent_revision"]["payload"] == parent["payload"]
    for row in graph["shot_revisions"]:
        assert row["payload"] == shots[row["shot_id"], row["revision_id"]]["payload"]
    for row in graph["internal_timeline_revisions"]:
        assert row["payload"] == internals[row["revision_id"]]["payload"]
    assert all("created_at" in row for row in [parent, *shots.values(), *internals.values()])
    assert (parent, shots, internals, document) == before
    projected.graph["parent_revision"]["payload"]["created_at"] = "caller mutation"
    assert (parent, shots, internals, document) == before
    assert runtime.service.get_project_parent_composition_revision(
        runtime.project_id, "main", runtime.golden_parent_revision_id)["payload"] == before[0]["payload"]


@pytest.mark.integration
@pytest.mark.parametrize("change", ["parent", "child", "new_shot", "duplicate", "remove", "sdk_bundle"])
def test_real_candidate_matches_sdk_opener_exactly(saved_closure, change):
    from astrid.core.timeline.authoring_bundle import open_authoring_bundle
    from astrid.sdk.timeline_editing import (
        add_authoring_shot,
        duplicate_authoring_shot,
        remove_authoring_shot,
    )
    from astrid.sdk.timeline_filmstrip import _open_candidate_input_closure

    runtime, document = saved_closure
    bundle = bundle_from_document(document)
    if change == "sdk_bundle":
        bundle = open_authoring_bundle(
            runtime.get_project_parent_composition_revision(
                runtime.project_id, "main", runtime.golden_parent_revision_id),
            shot_revisions=[runtime.get_project_shot_revision(runtime.project_id, row["shot_id"], row["revision_id"])
                            for row in document["shot_revisions"]],
            internal_timeline_revisions=[runtime.get_project_timeline_revision(runtime.project_id, "main", row["revision_id"])
                                         for row in document["internal_timeline_revisions"]])
        bundle["parent"]["config"]["opaque_draft"] = {"sdk": True}
    elif change == "parent":
        bundle["parent"]["config"]["opaque_draft"] = {"keep": [False, None, 0]}
    elif change == "child":
        next(iter(bundle["shots"].values()))["internal_timeline"]["clips"][0]["hold"] = 0.5
    elif change == "new_shot":
        add_authoring_shot(bundle, shot_id="new-shot", occurrence_id="new-occ", start_ms=2000)
    elif change == "duplicate":
        duplicate_authoring_shot(bundle, "shot-a", new_shot_id="duplicate-shot", occurrence_id="duplicate-occ", start_ms=2000)
    else:
        remove_authoring_shot(bundle, "shot-a")
    preview = preview_authoring_candidate(bundle)
    config, registry, expansion = _open_candidate_input_closure(
        runtime, project_id=runtime.project_id, timeline_id="main", preview=preview)
    assert project(document, preview) == {"config": config, "registry": registry, "expansion": expansion}
    assert runtime.service.get_project_timeline(
        runtime.project_id, "main")["head_revision_id"] == runtime.golden_parent_revision_id
