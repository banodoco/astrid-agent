"""The complete timing validation view preserves Main's canonical inputs."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.core.timeline.authoring_bundle import _digest, compile_authoring_candidate
from astrid.sdk.timeline._commit_timing_validation import validate_candidate_timing
from astrid.sdk.timeline.compiler import TimingCompileError, TimingInputs, compile_timing
from tests.editing_conformance.test_commit_v2_validation import candidate
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def test_original_main_rational_map_is_valid_and_byte_exact():
    from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle

    document = document_fixture(shared=False)
    value = bundle_from_document(document)
    before = encoded(value)
    publication = encoded(compile_authoring_candidate(value).publication)
    converted = encoded(document_from_bundle(value))
    with pytest.raises(TimingCompileError, match="canonical authoring adapter"):
        compile_timing(TimingInputs(value))
    validate_candidate_timing(value)
    assert encoded(value) == before
    assert encoded(compile_authoring_candidate(value).publication) == publication
    assert encoded(document_from_bundle(value)) == converted
    assert document_from_bundle(value) == document


def test_retained_nonunit_rational_rate_preserves_shared_occurrence_inputs():
    from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle

    document = document_fixture(shared=True)
    parent = document["parent_revision"]
    parent["payload"]["occurrences"][0]["speed"] = {"numerator": 3, "denominator": 2}
    parent["content_digest"] = _digest(parent["payload"])
    value = bundle_from_document(document)
    before = encoded(value)
    validate_candidate_timing(value)
    assert encoded(value) == before
    assert document_from_bundle(value) == document


@pytest.mark.parametrize("ownership", [
    "shared", "shared-patch", "shared-via-shot-revisions", "distinct-revision", "fork", "equal-forks",
])
def test_cut_identity_follows_physical_child_images_and_keeps_occurrences_local(ownership, monkeypatch):
    from astrid.sdk.timeline import _commit_timing_validation as adapter
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import _manifest, bundle_from_document, document_from_bundle
    from tests.editing_conformance.test_doc_patch_v2 import change, patch

    document = document_fixture(shared=True)
    internal = document["internal_timeline_revisions"][0]
    internal["payload"]["output"]["fps"] = 30
    document["parent_revision"]["payload"]["config"]["output"]["fps"] = 30
    internal["payload"]["clips"][0]["app"]["astrid"] = {"cut": "c1", "until": "end"}
    internal["content_digest"] = _digest(internal["payload"])
    if ownership in ("distinct-revision", "shared-via-shot-revisions"):
        # Identical bytes and the same timeline ID still describe two images.
        if ownership == "distinct-revision":
            second = copy.deepcopy(internal)
            second["revision_id"] = "internal-2"
            document["internal_timeline_revisions"].append(second)
        shot = copy.deepcopy(document["shot_revisions"][0])
        revision = "internal-2" if ownership == "distinct-revision" else "internal-1"
        shot.update(revision_id="shot-2", internal_timeline_revision_id=revision)
        shot["payload"]["internal_timeline_revision_id"] = revision
        shot["payload"]["name"] = "Another shot revision"
        shot["content_digest"] = _digest(shot["payload"])
        document["shot_revisions"].append(shot)
        document["parent_revision"]["payload"]["occurrences"][1]["shot_revision_id"] = "shot-2"
    document["parent_revision"]["content_digest"] = _digest(document["parent_revision"]["payload"])
    document["dependency_manifest"] = _manifest(
        document["shot_revisions"], document["internal_timeline_revisions"],
        document["dependency_manifest"]["media"], [],
    )
    value = bundle_from_document(document)
    if ownership == "shared-patch":
        value = execute(value, [patch(change(
            ["clips", "id:clip-a", "params"], {"state": "absent"}, {"x": 1},
            ref="internal-revision:internal-1",
        ))]).bundle
    elif ownership in ("fork", "equal-forks"):
        for shot in value["shots"].values():
            shot["internal_timeline"]["clips"][0]["params"] = {"x": 1}
            if ownership == "fork":
                break
    before = encoded(value)
    converted = encoded(document_from_bundle(value))
    results = []

    def capture(inputs):
        result = compile_timing(inputs)
        results.append(result)
        return result

    monkeypatch.setattr(adapter, "compile_timing", capture)
    if ownership in ("distinct-revision", "fork", "equal-forks"):
        with pytest.raises(TimingCompileError, match="ambiguous cut identity"):
            validate_candidate_timing(value)
    else:
        validate_candidate_timing(value)
        intervals = {owner[0]: interval for owner, interval in results[0].timings.items()
                     if owner[2] == "clip-a" and owner[0] != "parent"}
        assert [(intervals[occ]["start"], intervals[occ]["end"])
                for occ in ("occ-a", "occ-b")] == [(1, 10), (11, 20)]
        assert len(document_from_bundle(value)["internal_timeline_revisions"]) == 1
    assert encoded(value) == before
    assert encoded(document_from_bundle(value)) == converted


@pytest.mark.parametrize(("owner", "field", "value"), [
    ("parent", "for", 0), ("parent", "for", -1),
    ("parent", "on", "c999"), ("parent", "until", "c999"),
    ("child", "for", -1), ("child", "on", "c999"), ("child", "until", "c999"),
    ("parent", "at_ms", 1000), ("parent", "from_ms", 0),
    ("parent", "to_ms", 2000), ("parent", "time_map", {}),
])
def test_reviewer_invalid_timing_probes_reach_the_real_kernel(owner, field, value):
    bundle = candidate()
    rows = (bundle["parent"]["clips"] if owner == "parent" else
            bundle["shots"]["chapter-shot"]["internal_timeline"]["clips"])
    if field in ("for", "on", "until"):
        rows[0].setdefault("app", {}).setdefault("astrid", {})[field] = value
    else:
        rows[0][field] = value
        if field == "at_ms":
            rows[0].pop("at")
    if owner == "parent":
        bundle["parent"]["config"]["clips"] = copy.deepcopy(rows)
    before = encoded(bundle)
    with pytest.raises(TimingCompileError):
        validate_candidate_timing(bundle)
    assert encoded(bundle) == before


def test_child_declarations_are_not_filtered_and_valid_relations_are_resolved():
    value = candidate()
    child = value["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]
    child.setdefault("app", {})["astrid"] = {"cut": "c1", "for": 1}
    value["parent"]["clips"][0].setdefault("app", {})["astrid"] = {"on": "c1", "for": 1}
    value["parent"]["config"]["clips"] = copy.deepcopy(value["parent"]["clips"])
    before = encoded(value)
    validate_candidate_timing(value)
    assert encoded(value) == before
    for declaration in ({"on": 42}, {"until": []}, {"for": {}}, {"formulas": {"hold": 1}}):
        child["app"]["astrid"] = declaration
        before = encoded(value)
        with pytest.raises(TimingCompileError):
            validate_candidate_timing(value)
        assert encoded(value) == before


def test_explicit_clear_of_optional_canvas_container_survives_output_fps_adapter():
    value = candidate()
    value["parent"]["config"]["theme_overrides"] = None
    before = encoded(value)
    validate_candidate_timing(value)
    assert encoded(value) == before
    # This interval rounds to one frame at 30, but collapses at the exact
    # retained 30000/1001 rate. The real kernel must see output.fps.
    value["parent"]["clips"][0].update(at=0, hold=.01667)
    value["parent"]["config"]["clips"] = copy.deepcopy(value["parent"]["clips"])
    with pytest.raises(TimingCompileError, match="collapsed"):
        validate_candidate_timing(value)


def test_malformed_retained_placement_fields_are_not_hidden():
    from astrid.sdk.timeline.commit import bundle_from_document

    for field, malformed in (("speed", {"numerator": 1, "denominator": 0}),
                             ("source_offset", {"start": "bad", "end": 10000})):
        document = document_fixture(shared=False)
        # Supply an inherited malformed field, so the compiler's edit guard
        # alone cannot conceal a missing representation check in this adapter.
        parent = document["parent_revision"]
        parent["payload"]["occurrences"][0][field] = malformed
        parent["content_digest"] = _digest(parent["payload"])
        value = bundle_from_document(document)
        before = encoded(value)
        with pytest.raises(ValueError):
            validate_candidate_timing(value)
        assert encoded(value) == before


def test_existing_word_binding_adapter_uses_exact_retained_input_without_writes():
    from astrid.sdk.media_transcript import transcript_document_id
    from tests.sdk.test_timing_compiler import captured

    value = candidate()
    _, capture, _ = captured()
    document = capture["document"]
    document["project_id"] = value["project_id"]
    document["document_id"] = transcript_document_id(
        value["project_id"], document["content"]["media_id"], document["content"]["source_id"])
    capture["pin"]["document_id"] = document["document_id"]
    value["source_mapping"]["pinned_documents"] = [capture]
    internal = value["shots"]["chapter-shot"]["internal_timeline"]
    internal["clips"] = internal["clips"][:1]
    internal["clips"][0].update(**{"from": 2, "to": 3, "speed": 1})
    value["parent"]["clips"][0].setdefault("app", {})["astrid"] = {
        "on": '"again"', "for": .5}
    value["parent"]["config"]["clips"] = copy.deepcopy(value["parent"]["clips"])
    before = encoded(value)
    validate_candidate_timing(value)
    assert encoded(value) == before
    value["source_mapping"]["pinned_documents"] = []
    with pytest.raises(TimingCompileError, match="missing"):
        validate_candidate_timing(value)


def test_authored_word_reference_is_validated_instead_of_its_opaque_echo():
    from tests.editing_conformance.test_native_word_anchor_vectors import fixture

    value, pin, content = fixture()
    clip = value["parent"]["clips"][0]
    choice = {"source": "occurrence:occ-a/clip:voice", "document": pin,
              "word_ids": [content["segments"][0]["words"][0]["id"]]}
    clip["app"]["astrid"].update(on="opaque echo", word_anchors={"on": choice})
    value["parent"]["config"]["clips"] = copy.deepcopy(value["parent"]["clips"])
    before = encoded(value)
    validate_candidate_timing(value)
    assert encoded(value) == before
    choice["word_ids"] = ["word:" + "0" * 64]
    value["parent"]["config"]["clips"] = copy.deepcopy(value["parent"]["clips"])
    with pytest.raises(TimingCompileError, match="word is missing"):
        validate_candidate_timing(value)


def test_validation_has_no_io_or_mutable_checkout_dependency(monkeypatch):
    import builtins
    import importlib
    import socket
    import subprocess
    from pathlib import Path

    from astrid.sdk.timeline_checkout import Checkout

    importlib.import_module("astrid.sdk.timeline._timing_evaluators")
    value = candidate()
    before = encoded(value)

    def forbidden(*args, **kwargs):
        raise AssertionError("timing validation used IO or mutable Checkout execution")

    with monkeypatch.context() as patch:
        for obj, names in ((builtins, ["open"]), (Path, ["read_text", "read_bytes", "write_text"]),
                           (socket, ["socket", "create_connection"]), (subprocess, ["run", "Popen"]),
                           (Checkout, ["__init__", "check", "retime", "resolve", "reflow"])):
            for name in names:
                patch.setattr(obj, name, forbidden)
        validate_candidate_timing(value)
    assert encoded(value) == before
