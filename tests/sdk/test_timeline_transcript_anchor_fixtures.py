from __future__ import annotations

import hashlib
import json
from pathlib import Path


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/transcript_anchor_occurrence"
SOURCE_FIELDS = {"schema_version", "subject", "source", "clock", "words", "segments"}
WORD_FIELDS = {"id", "text", "start_s", "end_s"}
BINDING_FIELDS = {
    "schema_version", "document", "snapshot", "subject", "source", "clock",
    "occurrence_id", "word_ids",
}


def _read_json(name: str):
    return json.loads((FIXTURE_DIR / name).read_bytes())


def _source(corpus: dict, name: str) -> dict:
    return _read_json(corpus[name]["content_file"])


def _word_ids(source: dict) -> list[str]:
    return [word["id"] for word in source["words"]]


def _assert_source_record(source: dict) -> None:
    assert set(source) == SOURCE_FIELDS
    assert source["schema_version"] == 1
    assert source["clock"] == {"kind": "media", "unit": "seconds", "origin_seconds": 0}
    words = source["words"]
    ids = _word_ids(source)
    assert ids and all(isinstance(word_id, str) and word_id for word_id in ids)
    assert len(ids) == len(set(ids))
    for word in words:
        assert set(word) == WORD_FIELDS
        assert isinstance(word["start_s"], (int, float))
        assert isinstance(word["end_s"], (int, float))
        assert 0 <= word["start_s"] <= word["end_s"]

    segments = source["segments"]
    segment_ids = [segment["id"] for segment in segments]
    members = [word_id for segment in segments for word_id in segment["word_ids"]]
    assert len(segment_ids) == len(set(segment_ids))
    assert members == ids
    assert len(members) == len(set(members))
    assert all(set(segment) == {"id", "word_ids"} for segment in segments)


def _resolve_fixture_pointer(root: dict, pointer: str):
    assert pointer.startswith("#/")
    value = root
    for part in pointer[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[part]
    return value


def test_frozen_snapshot_bytes_and_digest_are_exact():
    corpus = _read_json("cases.json")
    record = corpus["frozen_source"]
    raw = (FIXTURE_DIR / record["content_file"]).read_bytes()
    source = json.loads(raw)

    _assert_source_record(source)
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == record["snapshot"]["sha256"]
    assert digest == record["document"]["content_digest"]
    assert record["document"]["version"] == 7
    assert record["snapshot"]["object_id"]
    assert "app.astrid.words" not in raw.decode("utf-8")
    assert "timeline_seconds" not in raw.decode("utf-8")


def test_same_lineage_resegmentation_keeps_retained_word_ids():
    corpus = _read_json("cases.json")
    original = _source(corpus, "frozen_source")
    correction = _source(corpus, "same_lineage_correction")
    original_source = original["source"]
    corrected_source = correction["source"]

    _assert_source_record(correction)
    assert corrected_source["source_id"] == original_source["source_id"]
    assert corrected_source["lineage_id"] == original_source["lineage_id"]
    assert corrected_source["revision_id"] != original_source["revision_id"]
    assert corrected_source["parent_revision_id"] == original_source["revision_id"]
    original_words = {word["id"]: word for word in original["words"]}
    corrected_words = {word["id"]: word for word in correction["words"]}
    retained_ids = set(original_words) & set(corrected_words)
    assert retained_ids == set(_word_ids(correction))
    assert all(original_words[word_id] == corrected_words[word_id] for word_id in retained_ids)
    assert set(original_words) - set(corrected_words) == {"fixture-word-a-009"}
    assert original["segments"] != correction["segments"]

    mapping = corpus["same_lineage_correction"]["retained_correspondence"]
    assert len(mapping) == len(retained_ids)
    assert len({(item["from"]["lineage_id"], item["from"]["word_id"]) for item in mapping}) == len(mapping)
    assert len({(item["to"]["lineage_id"], item["to"]["word_id"]) for item in mapping}) == len(mapping)
    assert all(set(item) == {"from", "to"} for item in mapping)
    assert all(item["from"] == item["to"] for item in mapping)


def test_repeated_occurrences_share_snapshot_and_keep_clip_inputs():
    corpus = _read_json("cases.json")
    source = _source(corpus, "frozen_source")
    valid_ids = set(_word_ids(source))
    occurrences = corpus["same_snapshot_occurrences"]
    assert len(occurrences) == 2
    assert len({item["target"]["clip_id"] for item in occurrences}) == 2
    assert len({item["target"]["occurrence_id"] for item in occurrences}) == 2

    for occurrence in occurrences:
        binding = occurrence["binding"]
        assert set(binding) == BINDING_FIELDS
        assert binding["schema_version"] == 1
        assert binding["document"] == corpus["frozen_source"]["document"]
        assert binding["snapshot"] == corpus["frozen_source"]["snapshot"]
        assert binding["subject"] == source["subject"]
        assert binding["source"] == source["source"]
        assert binding["clock"] == source["clock"]
        assert binding["occurrence_id"] == occurrence["target"]["occurrence_id"]
        assert set(binding["word_ids"]) <= valid_ids
        assert occurrence["target"]["clip_id"] == occurrence["clip"]["id"]
        assert set(occurrence["clip"]) == {"id", "asset", "at", "from", "to", "speed"}

    clips = [item["clip"] for item in occurrences]
    assert len({(clip["from"], clip["to"]) for clip in clips}) == 2
    assert len({clip["speed"] for clip in clips}) == 2
    assert len({clip["at"] for clip in clips}) == 2
    assert occurrences[0]["binding"]["word_ids"] != occurrences[1]["binding"]["word_ids"]


def test_new_take_has_distinct_identity_and_explicit_provenance_map():
    corpus = _read_json("cases.json")
    original = _source(corpus, "frozen_source")
    new_take = _source(corpus, "new_take")
    _assert_source_record(new_take)
    assert [word["text"] for word in new_take["words"]] == [word["text"] for word in original["words"]]
    assert set(_word_ids(new_take)).isdisjoint(_word_ids(original))
    assert new_take["subject"] == original["subject"]
    for field in ("source_id", "lineage_id", "revision_id"):
        assert new_take["source"][field] != original["source"][field]

    receipt = _resolve_fixture_pointer(corpus, corpus["take_correspondence"]["source_receipt"])
    assert receipt is corpus["new_take"]["binding"]
    assert receipt["document"] == corpus["new_take"]["document"]
    assert receipt["snapshot"] == corpus["new_take"]["snapshot"]
    assert receipt["source"] == new_take["source"]
    assert receipt["subject"] == new_take["subject"]

    mapping = corpus["take_correspondence"]["correspondence"]
    from_ids = [(item["from"]["lineage_id"], item["from"]["word_id"]) for item in mapping]
    to_ids = [(item["to"]["lineage_id"], item["to"]["word_id"]) for item in mapping]
    assert len(mapping) == len(original["words"]) == len(new_take["words"])
    assert len(from_ids) == len(set(from_ids))
    assert len(to_ids) == len(set(to_ids))
    assert all(set(item) == {"from", "to"} for item in mapping)
    assert all(set(item["from"]) == {"lineage_id", "word_id"} for item in mapping)
    assert all(set(item["to"]) == {"lineage_id", "word_id"} for item in mapping)
    old_words = {word["id"]: word["text"] for word in original["words"]}
    new_words = {word["id"]: word["text"] for word in new_take["words"]}
    assert all(item["from"]["lineage_id"] == original["source"]["lineage_id"] for item in mapping)
    assert all(item["to"]["lineage_id"] == new_take["source"]["lineage_id"] for item in mapping)
    assert all(old_words[item["from"]["word_id"]] == new_words[item["to"]["word_id"]] for item in mapping)


def test_ambiguous_duplicate_target_and_orphan_cases_are_explicit():
    corpus = _read_json("cases.json")
    ambiguous = corpus["ambiguous_repeated_text"]
    assert ambiguous["source_words"].count("we") == 2
    assert ambiguous["new_take_words"].count("we") == 2
    assert ambiguous["automatic_correspondence"] == []
    assert ambiguous["expected"].startswith("refuse automatic mapping")

    duplicate = corpus["duplicate_target_negative"]
    targets = [(item["to"]["lineage_id"], item["to"]["word_id"]) for item in duplicate["correspondence"]]
    assert len(targets) != len(set(targets))
    assert duplicate["expected"].startswith("reject")
    assert duplicate["source_receipt"] == corpus["take_correspondence"]["source_receipt"]

    orphan = corpus["unmatched_prior_anchor_orphan"]
    corrected = _source(corpus, "same_lineage_correction")
    assert orphan["prior_anchor"]["word_id"] not in set(_word_ids(corrected))
    assert orphan["correspondence"] == []
    assert orphan["expected_state"] == "ORPHAN"
    assert orphan["last_accepted_position"] == 42.25
    assert orphan["last_accepted_length"] == 0.70
