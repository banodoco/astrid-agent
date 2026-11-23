"""Exact, commit-only proofs for retiming existing legacy shot word anchors."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import cached_property

from astrid.core.timeline import moments
from astrid.sdk.media_transcript import transcript_document_id
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline._timing_evaluators import _duration, _fps
from astrid.sdk.timeline.compiler import TimingCompileError
from astrid.sdk.timeline.doc import _row_start
from astrid.sdk.timeline.native_word_anchor import binding
from astrid.sdk.timeline.units import _time_fraction, quantize_time, source_to_timeline_time
from astrid.sdk.timeline.words import as_words, default_source_order, project_words, reference_rows, timeline_media_ids


def _placement(bundle, shot):
    placement, = (row for row in bundle["placements"] if row["shot_id"] == shot)
    source_offset = placement.get("source_offset", placement.get("source_offset_ms"))
    if isinstance(source_offset, Mapping):
        source_offset = source_offset.get("start") if source_offset.get("end") == placement["duration_ms"] else None
        if source_offset is None:
            raise TimingCompileError("word retime requires a complete placement source window")
    speed = placement.get("speed")
    if isinstance(speed, Mapping):
        speed = _time_fraction(speed["numerator"], "speed numerator") / _time_fraction(
            speed["denominator"], "speed denominator")
    if (isinstance(source_offset, bool) or source_offset not in (None, 0)
            or speed is not None and _time_fraction(speed, "placement speed") != 1
            or any(placement.get(field) is not None for field in ("time_map", "timeMap"))):
        raise TimingCompileError("word retime requires a translation-only placement")
    return placement


def _start(placement, clip):
    start_ms = (placement.get("placement") or {}).get("start_ms", placement.get("at_ms", 0))
    return _time_fraction(start_ms, "occurrence start") / 1000 + _time_fraction(clip["at"], "clip start")


def select_retime_pins(bundle, pins):
    """Select one retained transcript for each audio media using Main's source identities."""
    selected = []
    references = reference_rows(bundle)
    source_order = default_source_order()
    for media in timeline_media_ids(bundle):
        identities = {row["documentId"] for row in references if row.get("mediaId") == media}
        if not identities:
            identities = {transcript_document_id(bundle["project_id"], media, source) for source in source_order}
        matches = [pin for pin in pins if pin["document_id"] in identities]
        if len(matches) != 1:
            return ()
        selected.append(matches[0])
    return selected


def retime_transcripts(capture_rows):
    captures = {}
    transcripts = {}
    for capture in capture_rows:
        document = capture["document"]
        if document["kind"] != "media.transcript":
            continue
        media, source = (document["content"][key] for key in ("media_id", "source_id"))
        if media in captures:
            raise TimingCompileError("word retime requires one exact transcript per audio source")
        captures[media] = capture
        transcripts[media] = {source: document}
    return transcripts, captures


def _word_pool(bundle):
    capture_rows = bundle.get("source_mapping", {}).get("pinned_documents", [])
    selected = select_retime_pins(bundle, [row["pin"] for row in capture_rows])
    transcripts, captures = retime_transcripts(row for row in capture_rows if row["pin"] in selected)
    if set(timeline_media_ids(bundle)) - captures.keys():
        raise TimingCompileError("word retime requires exact transcript coverage of the audio sources")
    # Use the same projection as reflow but require exactly one hit. Inline
    # words can make a match ambiguous but cannot prove an identity.
    return as_words(project_words(bundle, transcripts)), captures


def _exact_word(bundle, pool, owner, field, declaration):
    words, captures = pool
    moment = moments.parse(declaration)
    if moment.n not in (None, 1):
        raise TimingCompileError("word retime requires a unique word")
    phrase, = moments.find_words(moment, words)
    word = phrase[-1] if moment.edge == "end" else phrase[0]
    if not word.word_id or not word.media_id:
        raise TimingCompileError("word retime requires a captured transcript word")
    placements = sorted(bundle["placements"], key=_row_start)
    source_row = placements[word.occurrence]
    source = (source_row["occurrence_id"], source_row["shot_id"], word.clip_id)
    if word.shot_id != source[1] or any(
            (item.shot_id, item.occurrence, item.clip_id, item.media_id) !=
            (word.shot_id, word.occurrence, word.clip_id, word.media_id) for item in phrase):
        raise TimingCompileError("word retime requires one exact phrase source")
    pin = captures[word.media_id]["pin"]
    choice = {"source": f"occurrence:{source[0]}/clip:{source[2]}", "document": pin,
              "word_ids": [item.word_id for item in phrase]}
    exact = binding(bundle, "parent/clip:" + owner[2], field, choice, list(captures.values()))
    source_row = _placement(bundle, source[1])
    internal = bundle["shots"][source[1]]["internal_timeline"]
    source_clip, = (clip for clip in internal["clips"] if clip["id"] == source[2])
    content = captures[word.media_id]["document"]["content"]
    stored = {item["id"]: item for segment in content["segments"] for item in segment["words"]}
    start = _start(source_row, source_clip)
    end = start + _duration(source_clip)
    edges = [
        tuple(source_to_timeline_time(stored[item.word_id][edge], source_start=source_clip.get("from") or 0,
                                     timeline_start=start, speed=source_clip.get("speed") or 1)
              for edge in ("start", "end"))
        for item in phrase
    ]
    if any(not start <= onset < finish <= end for onset, finish in edges):
        raise TimingCompileError("word retime requires the whole phrase inside the played clip interval")
    edge = edges[-1][1] if moment.edge == "end" else edges[0][0]
    return replace(exact, target=owner, declaration=declaration), edge


class ShotWordRetimeInputs:
    """Share the projected transcript corpus across proofs for one bundle."""

    def __init__(self, bundle):
        self.bundle = bundle

    @cached_property
    def words(self):
        return _word_pool(self.bundle)

    def resolve(self, key, clip, field, text):
        _, shot, clip_id = key
        placement = _placement(self.bundle, shot)
        owner = (placement["occurrence_id"], shot, clip_id)
        start = _start(placement, clip)
        end = start + _duration(clip)
        exact, edge = _exact_word(self.bundle, self.words, owner, field, text)
        return exact, edge, (start, end)


def _mapping(bundle, key, clip):
    internal = bundle["shots"][key[1]]["internal_timeline"]
    asset = internal.get("registry", {}).get("assets", {}).get(clip.get("asset"))
    internal_id = bundle["source_mapping"]["shots"][key[1]].get("internal_timeline_id")
    return (internal_id, asset, {field: internal.get(field) for field in ("source_window", "time_map", "timeMap")})


def prove_shot_word_retime(candidate, baseline, key, clip, prior, field, text):
    """Return exact before/after bindings and authored intervals, or no proof."""
    if prior is None or _fps(candidate.bundle) != _fps(baseline.bundle):
        return None
    if any(prior.get(name) != clip.get(name) for name in (prior.keys() | clip.keys()) - {"at"}):
        return None
    try:
        if _mapping(baseline.bundle, key, prior) != _mapping(candidate.bundle, key, clip):
            return None
        before, old_edge, old_interval = baseline.resolve(key, prior, field, text)
        after, new_edge, new_interval = candidate.resolve(key, clip, field, text)
        if before != after or old_interval == new_interval:
            return None
        frame = 1 / _fps(candidate.bundle)
        if any(abs((new - new_edge) - (old - old_edge)) > frame
               for old, new in zip(old_interval, new_interval)):
            return None
    except (TimingCompileError, TimelineEditError, ArithmeticError, KeyError, TypeError, ValueError):
        return None
    return before, after, old_interval, new_interval


def confirm_shot_word_intervals(result, expected, fps):
    """Require that exact resolution preserves the stored start and end frames."""
    return all(owner in result.timings and all(
        quantize_time(result.timings[owner][edge], fps) == quantize_time(value, fps)
        for edge, value in zip(("start", "end"), interval)
    ) for owner, interval in expected.items())
