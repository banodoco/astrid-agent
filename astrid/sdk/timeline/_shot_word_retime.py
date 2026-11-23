"""Exact, commit-only proofs for retiming existing legacy shot word anchors."""
from __future__ import annotations

from collections.abc import Mapping
from functools import cached_property

from astrid.core.timeline import moments
from astrid.sdk.media_transcript import transcript_document_id
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline._timing_evaluators import SHOT_WORD_ALTERNATIVE, _ShotWordBinding, _duration, _fps
from astrid.sdk.timeline.compiler import TimingCompileError
from astrid.sdk.timeline.doc import _row_start
from astrid.sdk.timeline.native_word_anchor import binding
from astrid.sdk.timeline.units import _time_fraction, quantize_time, source_to_timeline_time
from astrid.sdk.timeline.words import (
    as_words, choose_document, default_source_order, project_words, reference_rows, timeline_media_ids,
    transcript_references,
)


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


def _word_source(bundle, source):
    _, shot, clip_id = source
    placement = _placement(bundle, shot)
    internal = bundle["shots"][shot]["internal_timeline"]
    clip, = (clip for clip in internal["clips"] if clip["id"] == clip_id)
    return placement, clip


def _word_source_mapping(bundle, source):
    placement, clip = _word_source(bundle, source)
    rate = _time_fraction(clip.get("speed") or 1, "speed")
    origin = source_to_timeline_time(0, source_start=clip.get("from") or 0,
                                     timeline_start=_start(placement, clip), speed=rate)
    return rate, origin


def retained_retime_pins(bundle):
    """Use the authenticated Runtime manifest, excluding request-only captures."""
    source = bundle.get("source_mapping", {})
    manifest = source.get("_runtime_document", {}).get("dependency_manifest", {})
    return [pin for pin in manifest.get("documents", []) if pin["document_id"].startswith("transcript:")]


def select_retime_pins(bundle, pins):
    """Find retained candidates, including explicit references without media metadata."""
    source_order = default_source_order()
    audio = set(timeline_media_ids(bundle))
    identities = {row["documentId"] for row in reference_rows(bundle)
                  if row.get("mediaId") is None or row["mediaId"] in audio} | {
        transcript_document_id(bundle["project_id"], media, source)
        for media in audio
        for source in source_order
    }
    return [pin for pin in pins if pin["document_id"] in identities]


def retime_transcripts(bundle, capture_rows):
    by_media = {}
    for capture in capture_rows:
        document = capture["document"]
        if document["kind"] != "media.transcript":
            continue
        media, source = (document["content"][key] for key in ("media_id", "source_id"))
        by_media.setdefault(media, {}).setdefault(source, []).append(capture)
    references = transcript_references(bundle)
    source_order = default_source_order()
    transcripts, captures = {}, {}
    for media, sources in by_media.items():
        docs = {source: rows[0]["document"] for source, rows in sources.items()}
        if sum(doc["document_id"] in references for doc in docs.values()) > 1:
            raise TimingCompileError("word retime requires one referenced transcript per audio media")
        chosen = choose_document(docs, references, source_order)
        if chosen is None:
            continue
        source, document = chosen
        if len(sources[source]) != 1:
            raise TimingCompileError("word retime requires one retained version of the selected transcript")
        capture = sources[source][0]
        captures[media] = capture
        transcripts[media] = {source: document}
    return transcripts, captures


def require_retime_word_scope(bundle, captures, line):
    """Prove that the declaration's managed audio search has exact retained coverage."""
    required = set(timeline_media_ids(bundle, line=line))
    present = {capture["pin"]["document_id"] for capture in captures.values()}
    missing_reference = any(
        row["documentId"] not in present
        and (row.get("mediaId") in required or (row.get("mediaId") is None and bool(required)))
        for row in reference_rows(bundle)
    )
    if required.difference(captures) or missing_reference:
        raise TimingCompileError(
            f"word retime requires retained transcripts for the word's audio search. {SHOT_WORD_ALTERNATIVE}"
        )


def _word_pool(bundle):
    capture_rows = bundle.get("source_mapping", {}).get("pinned_documents", [])
    selected = select_retime_pins(bundle, retained_retime_pins(bundle))
    transcripts, captures = retime_transcripts(bundle, (row for row in capture_rows if row["pin"] in selected))
    # Use the same projection as reflow but require exactly one hit. Inline
    # words can make a match ambiguous but cannot prove an identity.
    return as_words(project_words(bundle, transcripts)), captures


def _exact_word(bundle, pool, owner, field, declaration):
    words, captures = pool
    moment = moments.parse(declaration)
    require_retime_word_scope(bundle, captures, moment.line)
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
    source_row, source_clip = _word_source(bundle, source)
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
    projected = _ShotWordBinding(
        owner, field, declaration, exact.source, exact.document_id, exact.version,
        exact.content_digest, exact.word_ids, timeline_line=word.segment, first_audible=phrase[0].index == 0,
    )
    return projected, edge


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
        if before != after:
            return None
        # An occurrence resize can leave an earlier clip stationary. Require
        # the same word edge and source playback mapping in that case.
        if old_interval == new_interval:
            old_mapping = _word_source_mapping(baseline.bundle, before.source)
            new_mapping = _word_source_mapping(candidate.bundle, after.source)
            if old_edge != new_edge or old_mapping != new_mapping:
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
