"""Narrated P3 timeline examples against a scratch Runtime."""
from __future__ import annotations

import copy
from fractions import Fraction

import pytest

from astrid.sdk.timeline import commands
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.timeline.native_word_anchor import compile_authored
from tests.editing_conformance import native_word_anchor_fixture as runtime_fixture

TARGET = runtime_fixture.TARGET


@pytest.fixture
def narrated_timeline(tmp_path, monkeypatch, request):
    yield from runtime_fixture._retained_case(
        tmp_path, monkeypatch, request, current_form=True,
    )


def test_narrated_example_converts_captured_float_occurrence_start(narrated_timeline):
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

    case = narrated_timeline
    current = bundle_from_document(case.documents[case.head()["revision_id"]])
    current["source_mapping"]["pinned_documents"] = [{
        "pin": case.pin,
        "document": case.source,
    }]
    original = current["placements"][0]
    command = capture_occurrence_create(
        current, "float-occ", "float-shot", name="Voice", track="picture",
        on=10 / 3, duration=10,
    )
    captured = commands.execute(current, [command]).bundle
    occurrence = next(row for row in captured["placements"]
                      if row["occurrence_id"] == "float-occ")
    start_ms = occurrence["placement"]["start_ms"]
    assert isinstance(start_ms, float)

    # Apply the captured float to the source occurrence and publish the narrated
    # legacy anchor through the real Runtime validation path.
    after = copy.deepcopy(current)
    clips = after["parent"]["clips"]
    mirrors = after["parent"]["config"]["clips"]
    assert [clip["id"] for clip in clips] == [clip["id"] for clip in mirrors]
    for row in (clips[0], mirrors[0]):
        row["app"]["astrid"]["on"] = 'after "stop" -1f'

    patches = emit_document_commands(current, after)
    placement_patch = {
        "schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
        "args": {"changes": [{
            "op": "set", "ref": "parent",
            "path": ["occurrences", "occurrence_id:" + original["occurrence_id"],
                     "placement", "start_ms"],
            "before": {"state": "present", "value": original["placement"]["start_ms"]},
            "value": start_ms,
        }]},
    }
    accepted = case.submit(case.request([placement_patch, *patches]), "p3-float-timing")
    published = bundle_from_document(accepted["data"]["canonical_document"])
    published["source_mapping"]["pinned_documents"] = [{
        "pin": case.pin,
        "document": case.source,
    }]
    result = compile_authored(published, published["source_mapping"]["pinned_documents"])
    assert result.timings[TARGET]["start"] == Fraction(47047, 7500)
