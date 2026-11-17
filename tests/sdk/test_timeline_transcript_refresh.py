"""transcripts refresh: status per audio media, cut words, stale anchors, recorded references."""
from __future__ import annotations

from argparse import Namespace

import pytest

from astrid.sdk.media_transcript import IMPORT_SOURCE, transcript_document_id
from astrid.sdk.timeline import words as W
from astrid.sdk.timeline_transcript_refresh import cut_words, inventory, stale_anchors, with_reference

PROJECT = "project-1"
MEDIA_A = "sha256:" + "a" * 64
MEDIA_B = "sha256:" + "b" * 64
MEDIA_C = "sha256:" + "c" * 64


def _doc(media, source, words, version=2):
    content = {"schema": "media.transcript/v1", "media_id": media, "source_id": source,
               "segments": [{"id": "seg", "words": [
                   {"id": f"word:{i}", "start": s, "end": e, "text": t} for i, (s, e, t) in enumerate(words)]}]}
    return {"document_id": transcript_document_id(PROJECT, media, source), "version": version, "content": content}


def _clip(clip_id, *, at=0.0, src_from=0.0, to=None, asset="a", track="vo", **extra):
    row = {"id": clip_id, "track": track, "asset": asset, "at": at, "from": src_from, "speed": 1.0}
    if to is not None:
        row["to"] = to
    row.update(extra)
    return row


def _bundle(shots, parent_clips=()):
    shot_rows = {}
    placements = []
    for index, (sid, (media, clips)) in enumerate(shots.items()):
        shot_rows[sid] = {"internal_timeline": {"clips": clips, "tracks": [{"id": "vo", "kind": "vo"}],
                                                 "registry": {"assets": {"a": {"media_id": media}}}}}
        placements.append({"shot_id": sid, "occurrence_id": f"occ-{index}", "at_ms": index * 10000,
                           "duration_ms": 4000})
    return {"project_id": PROJECT, "placements": placements,
            "parent": {"clips": list(parent_clips), "tracks": [{"id": "vo", "kind": "vo"}],
                       "registry": {"assets": {"a": {"media_id": MEDIA_A}}}},
            "shots": shot_rows}


def test_inventory_reports_whisper_other_source_and_missing_media():
    whisper = W.default_source_order()[0]
    bundle = _bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)]),
                      "s2": (MEDIA_B, [_clip("c2", to=4.0)]),
                      "s3": (MEDIA_C, [_clip("c3", to=4.0)])})
    transcripts = {MEDIA_A: {whisper: _doc(MEDIA_A, whisper, [(0, 1, "x")])},
                   MEDIA_B: {IMPORT_SOURCE: _doc(MEDIA_B, IMPORT_SOURCE, [(0, 1, "y")])}}

    rows = {row["media_id"]: row for row in inventory(bundle, transcripts)}

    assert rows[MEDIA_A]["status"] == "whisper"
    assert rows[MEDIA_B]["status"] == f"other source only ({IMPORT_SOURCE})"
    assert rows[MEDIA_C]["status"] == "missing"
    assert rows[MEDIA_C]["version"] is None
    assert rows[MEDIA_A]["clips"] == ["s1/c1"]


def test_inventory_limits_to_one_shot_when_asked():
    bundle = _bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)]), "s2": (MEDIA_B, [_clip("c2", to=4.0)])})
    rows = inventory(bundle, {}, shot="s2")
    assert [row["media_id"] for row in rows] == [MEDIA_B]


def test_cut_words_reports_clip_edges_inside_a_word():
    whisper = W.default_source_order()[0]
    bundle = _bundle({"s1": (MEDIA_A, [_clip("c1", src_from=1.2, to=3.0)])})
    transcripts = {MEDIA_A: {whisper: _doc(MEDIA_A, whisper, [(1.0, 1.5, "hello"), (2.0, 2.5, "world")])}}

    cuts = cut_words(bundle, transcripts)

    assert [(c["edge"], c["text"], c["clip_id"]) for c in cuts] == [("in", "hello", "c1")]
    assert cuts[0]["point"] == pytest.approx(1.2)


def test_stale_anchor_is_reported_when_its_document_moved_on():
    whisper = W.default_source_order()[0]
    doc = _doc(MEDIA_A, whisper, [(1.0, 1.5, "hi")], version=5)
    anchored = {"id": "p1", "asset": "a", "at": 0, "from": 0, "to": 2, "track": "vo",
                "app": {"astrid": {"word_anchors": {"at": {"document": {
                    "document_id": doc["document_id"], "version": 4, "content_digest": "sha256:" + "d" * 64}}}}}}
    bundle = _bundle({}, parent_clips=[anchored])

    stale = stale_anchors(bundle, {MEDIA_A: {whisper: doc}})

    assert len(stale) == 1
    assert stale[0]["pinned_version"] == 4 and stale[0]["current_version"] == 5
    assert "older document version" in stale[0]["reason"]


def test_recorded_choice_replaces_only_this_media_association():
    other_doc = transcript_document_id(PROJECT, MEDIA_B, IMPORT_SOURCE)
    old_doc = transcript_document_id(PROJECT, MEDIA_A, IMPORT_SOURCE)
    new_doc = transcript_document_id(PROJECT, MEDIA_A, W.default_source_order()[0])
    bundle = {"parent": {"config": {"bundle": {"transcriptReferences": {
        "schema": "media.transcript.references/v1",
        "associations": [{"documentId": other_doc}, {"documentId": old_doc}]}}}}}

    rows = with_reference(bundle, {old_doc, new_doc}, new_doc)

    assert {"documentId": other_doc} in rows
    assert {"documentId": old_doc} not in rows
    assert rows[-1] == {"documentId": new_doc}


def test_run_missing_without_placement_is_refused(monkeypatch, capsys):
    from astrid.packs.timeline import cli_working
    from astrid.sdk.timeline_checkout import Checkout

    timeline = Checkout(_bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)])}))
    monkeypatch.setattr(Checkout, "open", classmethod(lambda cls, project, ref, client: timeline))
    args = Namespace(client=object(), project="p", ref="cut", shot=None, run_missing=True, placement=None,
                     dry_run=True, draft=None)

    assert cli_working._cmd_transcripts_refresh(args) == 2
    assert "--placement" in capsys.readouterr().err


def test_dry_run_prints_what_it_would_run_and_writes_nothing(monkeypatch, capsys):
    from astrid.packs.timeline import cli_working
    from astrid.sdk.timeline_checkout import Checkout

    timeline = Checkout(_bundle({"s1": (MEDIA_A, [_clip("c1", to=4.0)])}))
    monkeypatch.setattr(Checkout, "open", classmethod(lambda cls, project, ref, client: timeline))
    monkeypatch.setattr(cli_working, "working_copy", lambda parsed, create: (None, False))
    monkeypatch.setattr(W, "load_timeline_transcripts", lambda client, project, bundle, **kw: {})
    args = Namespace(client=object(), project="p", ref="cut", shot=None, run_missing=True, placement="runpod",
                     dry_run=True, draft=None)

    assert cli_working._cmd_transcripts_refresh(args) == 0
    out = capsys.readouterr().out
    assert f"would run: astrid timelines transcripts run {MEDIA_A} --project p --placement runpod" in out
    assert "dry run: nothing recorded" in out
