"""transcripts refresh writes an envelope the editor's strict parseTimelineBundle accepts."""
from __future__ import annotations

import copy
from argparse import Namespace

import pytest

from astrid.core.timeline.bundle import TimelineBundleValidationError, validate_timeline_bundle
from astrid.packs.timeline import cli_working
from astrid.sdk.timeline import words as W
from astrid.sdk.timeline_checkout import Checkout

from tests.sdk.test_timeline_transcript_refresh import MEDIA_A, MEDIA_B, _bundle, _clip, _FakeWorking, _stored

REFS = "media.transcript.references/v1"


def _run(monkeypatch, working, *, stored, timeline_bundle=None, created=None):
    whisper = W.default_source_order()[0]
    timeline = Checkout(timeline_bundle or _bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)])}))
    monkeypatch.setattr(Checkout, "open", classmethod(lambda cls, project, ref, client: timeline))
    monkeypatch.setattr(cli_working, "working_copy",
                        lambda parsed, create: (created.append(create) if created is not None else None, (working, True))[1])
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
    monkeypatch.setattr("astrid.sdk.timeline_transcript_refresh.resolve_asset_ids", lambda bundle, media: [])
    assert _run(monkeypatch, working, stored=stored) == 2  # the verb guard turns VerbError into exit 2
    assert "needs an assetId" in capsys.readouterr().err
    assert not working.saved
    assert "bundle" not in working.bundle["parent"].get("config", {})


def test_validator_accepts_optional_ids_and_rejects_missing_asset_id():
    base = {"schema_version": 1, "itemsBySchemaRef": {}}
    ok = {"schema": REFS, "associations": [{"assetId": "a", "documentId": "d"},
                                            {"assetId": "b", "documentId": "d", "mediaId": "m", "sourceId": "s"}]}
    validate_timeline_bundle({**base, "transcriptReferences": ok})
    bad = {"schema": REFS, "associations": [{"documentId": "d", "mediaId": "m", "sourceId": "s"}]}
    with pytest.raises(TimelineBundleValidationError, match="non-empty string"):
        validate_timeline_bundle({**base, "transcriptReferences": bad})
    with pytest.raises(TimelineBundleValidationError):
        validate_timeline_bundle({**base, "transcriptReferences": {"schema": REFS, "associations": [
            {"assetId": "a", "documentId": "d", "extra": 1}]}})
    with pytest.raises(TimelineBundleValidationError):
        validate_timeline_bundle({"transcriptReferences": ok})


def _timeline(parent_assets, shot_assets=None):
    """A parent clip on asset ``a`` (or, with ``shot_assets``, a shot clip on the shot's asset ``a``)."""
    if shot_assets is None:
        bundle = _bundle({}, parent_clips=[_clip("c1", to=4.0)])
    else:
        bundle = _bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)])})
        bundle["shots"]["s1"]["internal_timeline"]["registry"] = {"assets": shot_assets}
    bundle["parent"]["registry"] = {"assets": parent_assets}
    return bundle


def _refuses(monkeypatch, capsys, timeline_bundle):
    whisper = W.default_source_order()[0]
    created = []
    working = _FakeWorking(copy.deepcopy(timeline_bundle))
    assert _run(monkeypatch, working, stored=_stored_a(whisper), timeline_bundle=timeline_bundle, created=created) == 2
    assert True not in created and not working.saved
    return capsys.readouterr().err


def test_identity_is_media_id_or_content_sha_not_either(monkeypatch):
    from astrid.sdk.timeline_transcript_refresh import resolve_asset_ids

    bundle = _timeline({"a": {"media_id": "X", "content_sha256": MEDIA_A}, "b": {"content_sha256": MEDIA_A}})
    assert resolve_asset_ids(bundle, MEDIA_A) == ["b"]
    assert resolve_asset_ids(bundle, "X") == ["a"]


def test_shot_key_clash_with_parent_registry_refuses(monkeypatch, capsys):
    bundle = _timeline({"a": {"media_id": MEDIA_B}}, {"a": {"media_id": MEDIA_A}})
    err = _refuses(monkeypatch, capsys, bundle)
    assert "'a'" in err and "different media" in err


def test_shot_key_without_clash_resolves(monkeypatch):
    whisper = W.default_source_order()[0]
    bundle = _timeline({}, {"a": {"media_id": MEDIA_A}})
    working = _FakeWorking(_timeline({}, {"a": {"media_id": MEDIA_A}}))
    assert _run(monkeypatch, working, stored=_stored_a(whisper), timeline_bundle=bundle) == 0
    assert working.bundle["parent"]["config"]["bundle"]["transcriptReferences"]["associations"][0]["assetId"] == "a"


def test_same_media_under_two_keys_writes_one_row_per_key_and_is_idempotent(monkeypatch, capsys):
    whisper = W.default_source_order()[0]
    stored = _stored_a(whisper)
    spec = {"s1": {"media_id": MEDIA_A}, "a": {"media_id": MEDIA_A}, "media-0123": {"content_sha256": MEDIA_A}}
    bundle = _timeline({"a": spec["a"], "n": {"media_id": MEDIA_A}}, {"a": spec["a"], "media-0123": spec["media-0123"]})
    working = _FakeWorking(copy.deepcopy(bundle))
    assert _run(monkeypatch, working, stored=stored, timeline_bundle=bundle) == 0
    envelope = working.bundle["parent"]["config"]["bundle"]
    rows = envelope["transcriptReferences"]["associations"]
    doc = stored[MEDIA_A][whisper]["document_id"]
    assert [r["assetId"] for r in rows] == ["a", "n", "media-0123"]  # parent keys, then shot keys, sorted
    assert all(r["documentId"] == doc and r["mediaId"] == MEDIA_A and r["sourceId"] == whisper for r in rows)
    validate_timeline_bundle(envelope)
    # second run on the saved result: nothing to record
    capsys.readouterr()
    again = _FakeWorking(working.bundle)
    assert _run(monkeypatch, again, stored=stored, timeline_bundle=working.bundle) == 0
    assert "nothing to record" in capsys.readouterr().out and not again.saved


def test_legacy_row_matching_two_keys_expands_to_two_rows(monkeypatch):
    whisper = W.default_source_order()[0]
    bundle = _timeline({"a": {"media_id": MEDIA_A}, "m": {"media_id": MEDIA_B}, "m2": {"content_sha256": MEDIA_B}})
    bundle["parent"]["config"] = {"bundle": {"transcriptReferences": {"schema": REFS, "associations": [
        {"documentId": "doc-b", "mediaId": MEDIA_B, "sourceId": "src-b"}]}}}
    working = _FakeWorking(copy.deepcopy(bundle))
    assert _run(monkeypatch, working, stored=_stored_a(whisper), timeline_bundle=bundle) == 0
    rows = working.bundle["parent"]["config"]["bundle"]["transcriptReferences"]["associations"]
    assert rows[:2] == [{"assetId": k, "documentId": "doc-b", "mediaId": MEDIA_B, "sourceId": "src-b"} for k in ("m", "m2")]
    validate_timeline_bundle(working.bundle["parent"]["config"]["bundle"])


def test_validator_rejects_duplicate_asset_id_but_allows_shared_document():
    base = {"schema_version": 1, "itemsBySchemaRef": {}}
    ok = {"schema": REFS, "associations": [{"assetId": "a", "documentId": "d"}, {"assetId": "b", "documentId": "d"}]}
    validate_timeline_bundle({**base, "transcriptReferences": ok})
    dup = {"schema": REFS, "associations": [{"assetId": "a", "documentId": "d"}, {"assetId": "a", "documentId": "e"}]}
    with pytest.raises(TimelineBundleValidationError, match="duplicate assetId"):
        validate_timeline_bundle({**base, "transcriptReferences": dup})


def test_legacy_row_is_backfilled_keeping_media_and_source(monkeypatch):
    whisper = W.default_source_order()[0]
    working = _FakeWorking(_timeline({"a": {"media_id": MEDIA_A}, "m": {"media_id": MEDIA_B}}))
    working.bundle["parent"]["config"] = {"bundle": {"transcriptReferences": {"schema": REFS, "associations": [
        {"documentId": "doc-b", "mediaId": MEDIA_B, "sourceId": "src-b"}]}}}
    bundle = _timeline({"a": {"media_id": MEDIA_A}, "m": {"media_id": MEDIA_B}})
    assert _run(monkeypatch, working, stored=_stored_a(whisper), timeline_bundle=bundle) == 0
    envelope = working.bundle["parent"]["config"]["bundle"]
    assert envelope["transcriptReferences"]["associations"][0] == {
        "assetId": "m", "documentId": "doc-b", "mediaId": MEDIA_B, "sourceId": "src-b"}
    assert envelope["schema_version"] == 1
    validate_timeline_bundle(envelope)


def test_dead_legacy_row_names_the_row_and_leaves_no_working_copy(monkeypatch, capsys):
    working_bundle = _timeline({"a": {"media_id": MEDIA_A}})
    working_bundle["parent"]["config"] = {"bundle": {"transcriptReferences": {"schema": REFS, "associations": [
        {"documentId": "doc-gone", "mediaId": MEDIA_B, "sourceId": "src"}]}}}
    err = _refuses(monkeypatch, capsys, working_bundle)
    assert "doc-gone" in err and MEDIA_B in err and "remove the row or restore its media" in err


def test_validation_refusal_leaves_no_working_copy(monkeypatch, capsys):
    bundle = _timeline({"a": {"media_id": MEDIA_A}})
    bundle["parent"]["config"] = {"bundle": {"itemsBySchemaRef": {"x": "not-a-list"}}}
    err = _refuses(monkeypatch, capsys, bundle)
    assert "would not open in the editor" in err and "itemsBySchemaRef" in err


def test_editor_pin_on_the_medias_key_is_replaced_not_duplicated(monkeypatch):
    whisper = W.default_source_order()[0]
    stored = _stored_a(whisper)
    bundle = _timeline({"a": {"media_id": MEDIA_A}, "z": {"media_id": MEDIA_B}})
    bundle["parent"]["config"] = {"bundle": {"schema_version": 1, "itemsBySchemaRef": {}, "transcriptReferences": {
        "schema": REFS, "associations": [{"assetId": "a", "documentId": "other-source"},
                                         {"assetId": "z", "documentId": "doc-z"}]}}}
    working = _FakeWorking(copy.deepcopy(bundle))
    assert _run(monkeypatch, working, stored=stored, timeline_bundle=bundle) == 0
    rows = working.bundle["parent"]["config"]["bundle"]["transcriptReferences"]["associations"]
    assert [r for r in rows if r["assetId"] == "z"] == [{"assetId": "z", "documentId": "doc-z"}]
    mine = [r for r in rows if r["assetId"] == "a"]
    assert mine == [{"assetId": "a", "documentId": stored[MEDIA_A][whisper]["document_id"],
                     "mediaId": MEDIA_A, "sourceId": whisper}]
    assert len(rows) == 2
