"""transcripts refresh writes an envelope the editor's strict parseTimelineBundle accepts."""
from __future__ import annotations

from argparse import Namespace

import pytest

from astrid.core.timeline.bundle import TimelineBundleValidationError, validate_timeline_bundle
from astrid.packs.timeline import cli_working
from astrid.sdk.timeline import words as W
from astrid.sdk.timeline_checkout import Checkout

from tests.sdk.test_timeline_transcript_refresh import MEDIA_A, MEDIA_B, _bundle, _clip, _FakeWorking, _stored

REFS = "media.transcript.references/v1"


def _run(monkeypatch, working, *, stored, timeline_bundle=None):
    whisper = W.default_source_order()[0]
    timeline = Checkout(timeline_bundle or _bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)])}))
    monkeypatch.setattr(Checkout, "open", classmethod(lambda cls, project, ref, client: timeline))
    monkeypatch.setattr(cli_working, "working_copy", lambda parsed, create: (working, True))
    monkeypatch.setattr(W, "load_timeline_transcripts", lambda client, project, bundle, **kw: stored)
    args = Namespace(client=object(), project="p", ref="cut", shot=None, run_missing=False, placement=None,
                     dry_run=False, draft=None, use_source=whisper, use_document=None)
    return cli_working._cmd_transcripts_refresh(args)


def _stored_a(whisper):
    return {MEDIA_A: {whisper: _stored(MEDIA_A, whisper, [(0, 1, "x")])}}


def test_no_bundle_writes_full_envelope_with_asset_id(monkeypatch):
    whisper = W.default_source_order()[0]
    stored = _stored_a(whisper)
    working = _FakeWorking(_bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)])}))
    assert "bundle" not in working.bundle["parent"].get("config", {})
    assert _run(monkeypatch, working, stored=stored) == 0
    envelope = working.bundle["parent"]["config"]["bundle"]
    assert envelope["schema_version"] == 1
    assert envelope["itemsBySchemaRef"] == {}
    assert envelope["transcriptReferences"] == {"schema": REFS, "associations": [
        {"assetId": "a", "documentId": stored[MEDIA_A][whisper]["document_id"], "mediaId": MEDIA_A, "sourceId": whisper}]}
    validate_timeline_bundle(envelope)
    assert working.saved


def test_existing_bundle_is_kept_and_row_appended(monkeypatch):
    whisper = W.default_source_order()[0]
    stored = _stored_a(whisper)
    working = _FakeWorking(_bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)])}))
    working.bundle["parent"]["config"] = {"bundle": {
        "schema_version": 1, "itemsBySchemaRef": {}, "extra": 1,
        "transcriptReferences": {"schema": REFS, "associations": [
            {"assetId": "z", "documentId": "doc-other", "mediaId": MEDIA_B, "sourceId": "s"},
            {"documentId": "doc-old", "mediaId": MEDIA_A, "sourceId": "old"}]}}}
    assert _run(monkeypatch, working, stored=stored) == 0
    envelope = working.bundle["parent"]["config"]["bundle"]
    assert envelope["extra"] == 1
    rows = envelope["transcriptReferences"]["associations"]
    assert rows[0]["documentId"] == "doc-other"
    assert [r["mediaId"] for r in rows] == [MEDIA_B, MEDIA_A]  # old MEDIA_A pin replaced
    assert rows[1]["assetId"] == "a" and rows[1]["sourceId"] == whisper
    validate_timeline_bundle(envelope)


def test_unresolvable_asset_refuses_and_saves_nothing(monkeypatch, capsys):
    whisper = W.default_source_order()[0]
    stored = _stored_a(whisper)
    working = _FakeWorking(_bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)])}))
    # clips resolve their media through the registry, so an unresolvable id needs the resolver itself to fail
    monkeypatch.setattr("astrid.sdk.timeline_transcript_refresh.resolve_asset_id", lambda bundle, media: None)
    assert _run(monkeypatch, working, stored=stored) == 2  # the verb guard turns VerbError into exit 2
    assert "needs an assetId" in capsys.readouterr().err
    assert not working.saved
    assert "bundle" not in working.bundle["parent"].get("config", {})


def test_validator_accepts_optional_ids_and_rejects_missing_asset_id():
    base = {"schema_version": 1, "itemsBySchemaRef": {}}
    ok = {"schema": REFS, "associations": [{"assetId": "a", "documentId": "d"},
                                            {"assetId": "a", "documentId": "d", "mediaId": "m", "sourceId": "s"}]}
    validate_timeline_bundle({**base, "transcriptReferences": ok})
    bad = {"schema": REFS, "associations": [{"documentId": "d", "mediaId": "m", "sourceId": "s"}]}
    with pytest.raises(TimelineBundleValidationError, match="non-empty string"):
        validate_timeline_bundle({**base, "transcriptReferences": bad})
    with pytest.raises(TimelineBundleValidationError):
        validate_timeline_bundle({**base, "transcriptReferences": {"schema": REFS, "associations": [
            {"assetId": "a", "documentId": "d", "extra": 1}]}})
    with pytest.raises(TimelineBundleValidationError):
        validate_timeline_bundle({"transcriptReferences": ok})
