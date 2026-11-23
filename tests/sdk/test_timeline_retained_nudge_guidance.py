"""Unsupported public word-anchor nudges report cut-relative guidance."""
from __future__ import annotations

import contextlib
import copy
import re
from types import SimpleNamespace

import pytest

from astrid.core.timeline.authoring_bundle import _digest
from astrid.sdk.timeline import Timeline
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from tests.editing_conformance.test_shot_word_retime_refusal import GUIDANCE, _with_provable_anchor


def _canonical_nudge_inputs(word):
    bundle = _with_provable_anchor(word)
    document = document_from_bundle(bundle)
    # This is an inherited Runtime representation. The authoring API would
    # reject a request to introduce this placement map into an existing draft.
    parent = document["parent_revision"]
    placement = parent["payload"]["occurrences"][0]
    placement["source_offset"] = {"start": 0, "end": placement["duration_ms"]}
    placement["speed"] = {"numerator": 1, "denominator": 1}
    parent["content_digest"] = _digest(parent["payload"])
    capture, = bundle["source_mapping"]["pinned_documents"]
    return bundle_from_document(document), capture


def _open_retained_timeline(monkeypatch, bundle, capture):
    transport = SimpleNamespace(reads=[])

    def get_project_verified_document(project, pin):
        transport.reads.append((project, copy.deepcopy(pin)))
        return {"pin": copy.deepcopy(pin), "document": copy.deepcopy(transport.document)}

    transport.document = capture["document"]
    transport.get_project_verified_document = get_project_verified_document
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport))
    base = {"revision_id": "runtime-revision", "publication_id": "runtime-publication"}
    state = {"base": base, "pending": [], "flight": None}
    session = SimpleNamespace(
        project_id=bundle["project_id"], timeline_id=bundle["timeline_id"], client=client,
        outbox=SimpleNamespace(locked=contextlib.nullcontext),
        _state=lambda: state, resolve_acknowledged=lambda *a, **k: None,
        identity={"realm_id": "test-realm"},
    )

    monkeypatch.setattr("astrid.sdk.timeline.commit_session.TimelineSession.open", lambda *a, **k: session)
    monkeypatch.setattr(
        "astrid.sdk.timeline.store.fetch_bundle",
        lambda *a, **k: copy.deepcopy(bundle),
    )

    timeline = Timeline.open(bundle["project_id"], bundle["timeline_id"], client=client)
    return timeline, transport, capture


@pytest.mark.parametrize(("word", "selectors"), [
    pytest.param("stop", ("card",), id="clip-alone"),
    pytest.param("again", ("card", "voice"), id="ambiguous"),
])
def test_public_word_anchor_nudge_refuses_with_cut_relative_guidance(monkeypatch, word, selectors):
    bundle, capture = _canonical_nudge_inputs(word)
    timeline, transport, _ = _open_retained_timeline(monkeypatch, bundle, capture)
    latest_reads = []
    monkeypatch.setattr("astrid.sdk.media_transcript.read_transcript", lambda *args: latest_reads.append(args))

    working_timelines = []
    before = copy.deepcopy(timeline.bundle)
    state_before = copy.deepcopy(timeline.session._state())

    def shift(working):
        working_timelines.append(working)
        for selector in selectors:
            working.clip(selector).nudge(frames=15)

    with pytest.raises(TimelineEditError, match=re.escape(GUIDANCE)) as exc:
        timeline.commit(shift, label="Probe anchored retime", dry_run=True)

    if word == "again":
        assert "retained word passages" in str(exc.value)

    working, = working_timelines
    assert timeline.bundle == before
    assert timeline.session._state() == state_before
    assert working.bundle == before
    assert not working._commands and not working._steps
    assert "pinned_documents" not in working.bundle["source_mapping"]
    assert transport.reads == [(bundle["project_id"], capture["pin"])]
    assert latest_reads == []
