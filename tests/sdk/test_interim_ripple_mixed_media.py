"""Occurrence timing capture must retain the old mixed-edit closure guard."""
import copy

import pytest

from astrid.sdk.timeline import media_swap, publish, store
from astrid.sdk.timeline.commit import _digest
from astrid.sdk.timeline.emitter import DocumentPatchError
from tests.editing_conformance.test_interim_ripple_public import _open_film
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def _forbid_media_helpers(monkeypatch):
    def unexpected_helper(*args, **kwargs):
        pytest.fail("interim timing capture must not enter media helpers")

    monkeypatch.setattr(media_swap, "capture_document_swaps", unexpected_helper)
    monkeypatch.setattr(publish, "_media_admission_inputs", unexpected_helper)
    monkeypatch.setattr(store, "resolve_handle_entry", unexpected_helper)


@pytest.mark.parametrize("edit", ["literal-alias", "registered-rebinding", "registry-catalog", "clip-params"])
def test_timing_plus_unsupported_payload_keeps_old_guard(tmp_path, monkeypatch, edit):
    timeline, realm, calls = _open_film(tmp_path, monkeypatch, document_fixture(shared=False), None)
    original = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    _forbid_media_helpers(monkeypatch)

    def mixed_edit(working):
        placement = working.bundle["placements"][0]
        placement["placement"]["start_ms"] += 250
        internal = working.bundle["shots"][placement["shot_id"]]["internal_timeline"]
        clip = internal["clips"][0]
        assets = internal["registry"]["assets"]
        if edit == "literal-alias":
            clip["asset"] = "UNREGISTERED_ALIAS"
        elif edit == "registered-rebinding":
            assets[clip["asset"]]["media_id"] = "sha256:" + "2" * 64
        elif edit == "registry-catalog":
            assets["INJECTED_ALIAS"] = {
                "media_id": "sha256:" + "2" * 64, "type": "image",
                "generationId": "generation-new", "variantId": "variant-new",
            }
        else:
            clip["params"] = {"unrelated": "edit"}

    with pytest.raises(DocumentPatchError) as caught:
        timeline.commit(mixed_edit, label="Mixed payload and occurrence timing", dry_run=True)

    assert type(caught.value) is DocumentPatchError
    assert str(caught.value) == "closure membership or provenance changed; use a semantic command"
    assert calls == []
    assert realm.accepted == []
    assert realm.documents[realm.head["revision_id"]] == original


@pytest.mark.parametrize("edit", ["clip-hold", "source-from", "source-to", "inline-words", "clip-removal"])
def test_timing_with_existing_media_payload_keeps_old_guard(tmp_path, monkeypatch, edit):
    document = document_fixture(shared=False)
    internal, = document["internal_timeline_revisions"]
    clip = internal["payload"]["clips"][0]
    clip.update({"from": 0, "to": 2})
    clip["app"]["astrid"] = {"words": [[0, 0.5, "one"], [0.6, 1.0, "two"]]}
    internal["content_digest"] = _digest(internal["payload"])
    document["dependency_manifest"]["internal_timelines"][0]["content_digest"] = internal["content_digest"]
    timeline, realm, calls = _open_film(tmp_path, monkeypatch, document, None)
    original = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    _forbid_media_helpers(monkeypatch)

    def mixed_edit(working):
        placement = working.bundle["placements"][0]
        placement["placement"]["start_ms"] += 250
        clips = working.bundle["shots"][placement["shot_id"]]["internal_timeline"]["clips"]
        if edit == "clip-hold":
            clips[0]["hold"] -= 1
        elif edit == "source-from":
            clips[0]["from"] = 0.25
        elif edit == "source-to":
            clips[0]["to"] = 1.75
        elif edit == "inline-words":
            clips[0]["app"]["astrid"]["words"] = [[0.1, 0.6, "one"]]
        else:
            clips.pop()

    with pytest.raises(DocumentPatchError) as caught:
        timeline.commit(mixed_edit, label="Mixed payload and occurrence timing", dry_run=True)

    assert type(caught.value) is DocumentPatchError
    assert str(caught.value) == "closure membership or provenance changed; use a semantic command"
    assert calls == []
    assert realm.accepted == []
    assert realm.documents[realm.head["revision_id"]] == original
