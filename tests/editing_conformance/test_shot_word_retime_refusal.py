"""Commit validation of retimes for legacy word-anchored shot clips."""
from __future__ import annotations

import copy
import re

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError, _digest
from astrid.sdk.media_transcript import build_transcript, transcript_document_id
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.validation import validate_final_candidate
from astrid.sdk.timeline_checkout import Checkout
from tests.editing_conformance.test_native_word_anchor_vectors import fixture


GUIDANCE = "Use a cut-relative timing declaration such as +0.8s."


def _with_legacy_anchor(word="stop"):
    bundle, _, _ = fixture()
    internal = bundle["shots"]["chapter-shot"]["internal_timeline"]
    voice = internal["clips"][0]
    voice["app"] = {"astrid": {"cut": "c1"}}
    internal["tracks"].append({"id": "overlay", "kind": "visual"})
    internal["registry"]["assets"]["still"] = {
        **internal["registry"]["assets"]["picture"],
        "type": "image",
    }
    internal["clips"].append({
        "id": "card", "track": "overlay", "asset": "still", "clipType": "media",
        "at": 2.75 if word == "stop" else 1.5, "hold": 0.5,
        "app": {"astrid": {"cut": "c1", "on": f'"{word}"', "for": 0.5}},
    })
    return bundle


def _shot_clips(bundle):
    return bundle["shots"]["chapter-shot"]["internal_timeline"]["clips"]


@pytest.mark.parametrize(("word", "move_narration"), [
    pytest.param("stop", False, id="unique-clip-only"),
    pytest.param("stop", True, id="unique-ripple"),
    pytest.param("again", True, id="ambiguous-ripple"),
])
def test_retiming_a_legacy_word_anchored_shot_clip_is_refused(word, move_narration):
    baseline = _with_legacy_anchor(word)
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)
    card["at"] += 0.5
    if move_narration:
        voice["at"] += 0.5

    before = copy.deepcopy(candidate)
    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)
    assert candidate == before


def test_cut_relative_ripple_passes_commit_validation():
    baseline = _with_legacy_anchor()
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)
    voice["at"] += 0.5
    card["at"] += 0.5
    card["app"]["astrid"]["on"] = "+1.75s"

    validate_final_candidate(candidate, baseline=baseline)


def test_changing_declared_duration_alone_is_refused():
    baseline = _with_legacy_anchor()
    candidate = copy.deepcopy(baseline)
    _shot_clips(candidate)[1]["app"]["astrid"]["for"] = 0.75

    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)


def test_explicit_shot_word_choice_has_the_same_cut_relative_alternative():
    baseline = _with_legacy_anchor()
    candidate = copy.deepcopy(baseline)
    capture, = baseline["source_mapping"]["pinned_documents"]
    stop = capture["document"]["content"]["segments"][0]["words"][-1]
    _shot_clips(candidate)[1]["app"]["astrid"]["word_anchors"] = {"on": {
        "document": capture["pin"], "source": "occurrence:occ-a/clip:voice", "word_ids": [stop["id"]],
    }}

    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)


def test_unrelated_parent_edit_preserves_legacy_shot_anchor():
    baseline = _with_legacy_anchor()
    candidate = copy.deepcopy(baseline)
    candidate["parent"]["config"]["tracks"].append({"id": "new", "kind": "audio"})

    validate_final_candidate(candidate, baseline=baseline)
    assert _shot_clips(candidate) == _shot_clips(baseline)


def _with_provable_anchor(word="stop"):
    bundle = _with_legacy_anchor(word)
    voice, card = _shot_clips(bundle)
    voice.pop("app")
    card.pop("asset")
    card["clipType"] = "text"
    card["at"] = 82 / 30 if word == "stop" else 1
    capture, = bundle["source_mapping"]["pinned_documents"]
    document = capture["document"]
    content = document["content"]
    config = bundle["parent"]["config"]
    config["output"]["fps"] = 30
    config["theme_overrides"]["visual"]["canvas"]["fps"] = 30
    config.setdefault("bundle", {})["transcriptReferences"] = {"associations": [{
        "documentId": document["document_id"], "mediaId": content["media_id"], "sourceId": content["source_id"],
    }]}
    # The native word vectors intentionally disagree about this parent clip's
    # physical and authored duration. Start the ripple fixtures in agreement.
    for parent_clip in (bundle["parent"]["clips"][0], config["clips"][0]):
        parent_clip["hold"] = parent_clip["app"]["astrid"]["for"]
    return bundle


def test_exact_word_ripple_passes_and_reflow_moves_no_clip(monkeypatch):
    baseline = _with_provable_anchor()
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)
    voice["at"] += 0.5
    card["at"] += 0.5

    validate_final_candidate(candidate, baseline=baseline)

    document = candidate["source_mapping"]["pinned_documents"][0]["document"]
    content = document["content"]
    monkeypatch.setattr(Checkout, "_stored_transcripts", lambda self: {
        content["media_id"]: {content["source_id"]: document},
    })
    checkout = Checkout(copy.deepcopy(candidate))
    assert checkout.reflow() == []


@pytest.mark.parametrize("case", ["clip-only", "ambiguous", "drifted-before", "source-map", "duration", "ordinal"])
def test_available_transcript_does_not_exempt_unproven_retime(case):
    baseline = _with_provable_anchor("again" if case == "ambiguous" else "stop")
    if case == "drifted-before":
        _shot_clips(baseline)[1]["at"] += 0.1
    if case == "ordinal":
        _shot_clips(baseline)[1]["app"]["astrid"]["on"] = '"stop" #0'
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)
    card["at"] += 0.5
    if case != "clip-only":
        voice["at"] += 0.5
    if case == "source-map":
        card["speed"] = 2
    if case == "duration":
        card["hold"] = card["app"]["astrid"]["for"] = 0.75

    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)


def test_malformed_explicit_shot_reference_has_cut_relative_guidance():
    baseline = _with_provable_anchor()
    candidate = copy.deepcopy(baseline)
    _shot_clips(candidate)[1]["app"]["astrid"]["word_anchors"] = []

    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)


def _canonical_reflow_inputs():
    bundle = _with_provable_anchor()
    document = document_from_bundle(bundle)
    # This is an inherited Runtime representation. The authoring API would
    # reject a request to introduce this placement map into an existing draft.
    parent = document["parent_revision"]
    placement = parent["payload"]["occurrences"][0]
    placement["source_offset"] = {"start": 0, "end": placement["duration_ms"]}
    placement["speed"] = {"numerator": 1, "denominator": 1}
    parent["content_digest"] = _digest(parent["payload"])
    captures = bundle["source_mapping"]["pinned_documents"]
    sealed = bundle_from_document(document)
    sealed["source_mapping"]["pinned_documents"] = captures
    return sealed, captures[0]


class _TranscriptPort:
    def __init__(self, exact, latest):
        self.exact, self.latest, self.reads = exact, latest, []
        self.documents = self

    def get_project_verified_document(self, project, pin):
        self.reads.append((project, pin))
        if self.exact is None:
            raise RuntimeError("exact transcript unavailable")
        return self.exact

    def show(self, *args, **kwargs):
        self.reads.append("latest")
        return self.latest


def test_placement_ripple_with_canonical_maps_uses_retained_version_for_noop_reflow():
    baseline, capture = _canonical_reflow_inputs()
    candidate = copy.deepcopy(baseline)
    candidate["placements"][0]["placement"]["start_ms"] += 500
    validate_final_candidate(candidate, baseline=baseline)

    # The current transcript has moved the word by half a second, but it is
    # outside the timeline's retained input closure.
    latest = copy.deepcopy(capture["document"])
    latest["version"] += 1
    latest["content"]["segments"][0]["words"][-1].update(start=5, end=5.5)
    checkout = Checkout(bundle_from_document(document_from_bundle(candidate)))
    checkout._scene_client = port = _TranscriptPort(capture, latest)
    checkout._scene_project = candidate["project_id"]
    assert checkout.reflow() == []
    assert port.reads == [(candidate["project_id"], capture["pin"])]


def test_reflow_with_missing_exact_capture_refuses_before_using_latest():
    bundle, capture = _canonical_reflow_inputs()
    checkout = Checkout(bundle)
    checkout._scene_client = port = _TranscriptPort(None, capture["document"])
    checkout._scene_project = bundle["project_id"]
    baseline = copy.deepcopy(checkout.bundle)

    with pytest.raises(TimelineEditError, match="exact transcript unavailable"):
        checkout.reflow()
    assert checkout.bundle == baseline
    assert port.reads == [(bundle["project_id"], capture["pin"])]


def test_unproven_placement_ripple_is_refused():
    baseline = _with_legacy_anchor()
    candidate = copy.deepcopy(baseline)
    candidate["placements"][0]["placement"]["start_ms"] += 500

    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)


@pytest.mark.parametrize("move_end_independently", [False, True])
def test_ripple_resolves_both_word_endpoints(move_end_independently):
    baseline = _with_provable_anchor()
    _, card = _shot_clips(baseline)
    app = card["app"]["astrid"]
    app.pop("for")
    app["until"] = 'after "stop" +7f'
    card["hold"] = 0.5
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)
    voice["at"] += 0.5
    card["at"] += 0.5

    if move_end_independently:
        # Keep the start word at its old-relative location while moving the
        # word's end independently via a different narration playback rate.
        voice["speed"] = 4
        voice["at"] += 0.875
        with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
            validate_final_candidate(candidate, baseline=baseline)
    else:
        validate_final_candidate(candidate, baseline=baseline)


def test_ripple_cannot_use_a_different_captured_transcript_version():
    baseline = _with_provable_anchor()
    candidate = copy.deepcopy(baseline)
    for clip in _shot_clips(candidate):
        clip["at"] += 0.5
    capture, = candidate["source_mapping"]["pinned_documents"]
    capture["pin"]["version"] += 1
    capture["document"]["version"] += 1

    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)


def test_word_ripple_preserves_shot_footage_source_and_duration():
    baseline = _with_provable_anchor()
    _, picture = _shot_clips(baseline)
    picture.update(clipType="media", asset="still")
    candidate = copy.deepcopy(baseline)
    for clip in _shot_clips(candidate):
        clip["at"] += 0.5
    expected = copy.deepcopy(candidate)

    validate_final_candidate(candidate, baseline=baseline)
    assert candidate == expected


def test_partial_retained_manifest_reads_the_retained_narration_without_latest():
    bundle, capture = _canonical_reflow_inputs()
    # A second managed audio source has no exact transcript in the manifest.
    # Its presence must not make reflow discard the narration's retained pin.
    internal = bundle["shots"]["chapter-shot"]["internal_timeline"]
    internal["registry"]["assets"]["untracked-audio"] = {"media_id": "sha256:" + "c" * 64}
    internal["clips"].append({"id": "other-voice", "track": "voice", "asset": "untracked-audio",
                              "clipType": "media", "at": 0, "from": 0, "to": 2})
    latest = copy.deepcopy(capture["document"])
    latest["version"] += 1
    latest["content"]["segments"][0]["words"][-1].update(start=5, end=5.5)
    checkout = Checkout(bundle)
    checkout._scene_client = port = _TranscriptPort(capture, latest)
    checkout._scene_project = bundle["project_id"]

    assert checkout.reflow() == []
    assert port.reads == [(bundle["project_id"], capture["pin"])]


@pytest.mark.parametrize("word", ["stop", "halt"])
def test_document_id_only_transcript_reference_selects_the_explicit_custom_document(word):
    baseline = _with_provable_anchor()
    capture, = baseline["source_mapping"]["pinned_documents"]
    custom = copy.deepcopy(capture)
    media = custom["document"]["content"]["media_id"]
    custom_id = transcript_document_id(baseline["project_id"], media, "custom")
    content = build_transcript(media, "line", "again again halt",
                               [[2, 2.5, "again"], [3, 3.5, "again"], [5.5, 6, "halt"]],
                               source_id="custom")
    custom["document"].update(document_id=custom_id, content=content)
    custom["pin"].update(document_id=custom_id, content_digest=_digest(content))
    baseline["source_mapping"]["pinned_documents"].append(custom)
    baseline["parent"]["config"]["bundle"]["transcriptReferences"]["associations"] = [{
        "documentId": custom_id,
    }]
    _shot_clips(baseline)[1]["app"]["astrid"]["on"] = f'"{word}"'
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)
    voice["at"] += 0.5
    card["at"] += 0.5

    if word == "halt":
        validate_final_candidate(candidate, baseline=baseline)
    else:
        with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
            validate_final_candidate(candidate, baseline=baseline)


@pytest.mark.parametrize("anchor_at_word", [False, True])
def test_word_ripple_on_first_audible_word_of_trimmed_narration_reflows_in_place(monkeypatch, anchor_at_word):
    baseline = _with_provable_anchor()
    voice, card = _shot_clips(baseline)
    voice.update({"from": 5, "at": 4, "app": {"astrid": {"segment": "voice"}}})
    internal = baseline["shots"]["chapter-shot"]["internal_timeline"]
    internal["tracks"].append({"id": "plate", "kind": "visual"})
    # A later literal picture already ends at the placement bound, so reflow
    # has no unrelated film-length adjustment to make.
    internal["clips"].append({"id": "last-plate", "track": "plate", "clipType": "text",
                              "at": 8, "hold": 2})
    card["track"] = "plate"
    # The take's in-point precedes its first audible word, which was not the
    # first word of the untrimmed transcript.
    card["at"] = 127 / 30 if anchor_at_word else 1.5
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)[:2]
    voice["at"] += 0.5
    card["at"] += 0.5

    if anchor_at_word:
        with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
            validate_final_candidate(candidate, baseline=baseline)
        return
    validate_final_candidate(candidate, baseline=baseline)

    document = candidate["source_mapping"]["pinned_documents"][0]["document"]
    content = document["content"]
    monkeypatch.setattr(Checkout, "_stored_transcripts", lambda self: {
        content["media_id"]: {content["source_id"]: document},
    })
    checkout = Checkout(copy.deepcopy(candidate))
    assert checkout.reflow() == []


def test_line_qualified_ripple_accepts_timeline_line_id_distinct_from_segment_id():
    baseline = _with_provable_anchor()
    _, card = _shot_clips(baseline)
    card["app"]["astrid"]["on"] = '"stop" in voice'
    # The fixture already has transcript segment id "line" and timeline id "voice".
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)
    voice["at"] += 0.5
    card["at"] += 0.5

    validate_final_candidate(candidate, baseline=baseline)
