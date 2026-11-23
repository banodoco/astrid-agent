"""Occurrence timing capture must retain the old mixed-edit closure guard."""
import copy

import pytest

from astrid.sdk.timeline import media_swap, publish, store
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


def test_timing_with_existing_clip_trim_and_removal_skips_media_helpers(tmp_path, monkeypatch):
    timeline, realm, calls = _open_film(tmp_path, monkeypatch, document_fixture(shared=False), None)
    _forbid_media_helpers(monkeypatch)

    def edit(working):
        placement = working.bundle["placements"][0]
        placement["duration_ms"] -= 1000
        clips = working.bundle["shots"][placement["shot_id"]]["internal_timeline"]["clips"]
        clips[0]["hold"] -= 1
        clips.pop()

    preview = timeline.commit(edit, label="Trim and remove existing clips", dry_run=True)

    assert preview["data"]["would_status"] == "accepted"
    assert preview["receipt"] is None
    assert len(calls) == 1 and calls[0]["dry_run"]
    canonical = preview["data"]["canonical_document"]
    occurrence, = canonical["parent_revision"]["payload"]["occurrences"]
    assert occurrence["duration_ms"] == 9000
    internal, = canonical["internal_timeline_revisions"]
    clip, = internal["payload"]["clips"]
    assert (clip["id"], clip["asset"], clip["hold"]) == ("clip-a", "picture", 1)
    assert not realm.accepted
