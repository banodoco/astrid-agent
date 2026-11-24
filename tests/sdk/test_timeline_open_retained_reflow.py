"""The public Timeline opener retains its Runtime client for anchored reflow."""
from __future__ import annotations

import contextlib
import copy
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline import Timeline
from astrid.sdk.timeline._common import TimelineEditError
from tests.editing_conformance.test_shot_word_retime_refusal import _canonical_reflow_inputs


@pytest.fixture
def retained_timeline(monkeypatch):
    bundle, capture = _canonical_reflow_inputs()
    transport = SimpleNamespace(reads=[])

    def get_project_verified_document(project, pin):
        transport.reads.append((project, copy.deepcopy(pin)))
        return {"pin": copy.deepcopy(pin), "document": copy.deepcopy(transport.document)}

    transport.document = capture["document"]
    transport.get_project_verified_document = get_project_verified_document
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport))
    base = {"revision_id": "runtime-revision", "publication_id": "runtime-publication"}
    session = SimpleNamespace(
        project_id=bundle["project_id"], timeline_id=bundle["timeline_id"], client=client,
        outbox=SimpleNamespace(locked=contextlib.nullcontext),
        _state=lambda: {"base": base},
    )

    monkeypatch.setattr("astrid.sdk.timeline.commit_session.TimelineSession.open", lambda *a, **k: session)
    monkeypatch.setattr(
        "astrid.sdk.timeline.store.fetch_bundle",
        lambda *a, **k: copy.deepcopy(bundle),
    )

    timeline = Timeline.open(bundle["project_id"], bundle["timeline_id"], client=client)
    return timeline, transport, capture


def test_timeline_open_reflow_reads_exact_retained_transcript(retained_timeline):
    timeline, transport, capture = retained_timeline
    bundle = timeline.bundle

    assert timeline.reflow() == []
    assert transport.reads == [(bundle["project_id"], capture["pin"])]


def test_timeline_open_reflow_refuses_mismatched_retained_transcript(retained_timeline):
    timeline, transport, capture = retained_timeline
    pin = capture["pin"]
    shadow_pin = {**pin, "version": pin["version"] + 1}
    timeline.bundle["source_mapping"]["_runtime_document"] = {"dependency_manifest": {"documents": [shadow_pin]}}
    transport.document = {**transport.document, "version": shadow_pin["version"]}
    before = copy.deepcopy(timeline.bundle)

    with pytest.raises(TimelineEditError, match="scope or version does not match its pin"):
        timeline.reflow()

    assert timeline.bundle == before
    assert transport.reads == [(timeline.bundle["project_id"], pin)]
