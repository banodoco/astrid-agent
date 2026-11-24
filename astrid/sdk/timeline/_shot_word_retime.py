"""Exact, commit-only proofs for retiming existing legacy shot word anchors."""
from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from functools import cached_property
from typing import NamedTuple

from astrid.core.timeline import moments
from astrid.sdk import timeline_intent as intent
from astrid.sdk.media_transcript import transcript_document_id
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline._recording import active
from astrid.sdk.timeline._timing_evaluators import (
    SHOT_WORD_ALTERNATIVE,
    _duration,
    _fps,
    _ShotWordBinding,
)
from astrid.sdk.timeline.compiler import TimingCompileError
from astrid.sdk.timeline.doc import _row_start
from astrid.sdk.timeline.native_word_anchor import binding
from astrid.sdk.timeline.units import _time_fraction, quantize_time, source_to_timeline_time
from astrid.sdk.timeline.words import (
    _clip_media_id,
    _holder_clips,
    _holder_tracks,
    _occurrences,
    _registry_assets,
    _source_window,
    _track_is_audio,
    as_words,
    choose_document,
    default_source_order,
    project_words,
    reference_rows,
    timeline_media_ids,
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


def retained_retime_pins(runtime_document):
    """Use the authenticated Runtime manifest, excluding request-only captures."""
    manifest = (runtime_document or {}).get("dependency_manifest", {})
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
    source = bundle.get("source_mapping", {})
    capture_rows = source.get("pinned_documents", [])
    selected = select_retime_pins(bundle, retained_retime_pins(source.get("_runtime_document")))
    transcripts, captures = retime_transcripts(bundle, (row for row in capture_rows if row["pin"] in selected))
    # Use the same projection as reflow but require exactly one hit. Inline
    # words can make a match ambiguous but cannot prove an identity.
    return as_words(project_words(bundle, transcripts)), captures


class _ResolvedWord(NamedTuple):
    binding: _ShotWordBinding
    edge: Fraction
    interval: tuple[Fraction, Fraction]
    source_timing: tuple[Fraction, Fraction, Fraction, Fraction]


class _WordSearchInputs(NamedTuple):
    audio: dict
    references: list
    pins: list
    captures: list

    def retains_referenced_pins(self, prior):
        """Allow enrolling a previously authored reference without changing its search."""
        if self.audio != prior.audio or self.references != prior.references:
            return False
        if any(pin not in self.pins for pin in prior.pins) or any(
            pin not in self.captures for pin in prior.captures
        ):
            return False
        references = {row["documentId"] for row in prior.references}
        retained = {pin["document_id"] for pin in prior.pins}
        return (
            all(pin in prior.pins or pin["document_id"] in references - retained for pin in self.pins)
            and all(pin in prior.captures or pin["document_id"] in references for pin in self.captures)
        )


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
    rate = _time_fraction(source_clip.get("speed") or 1, "speed")
    source_start = source_clip.get("from") or 0
    if source_clip.get("to") is not None:
        # A hold controls the clip span, but playback still stops at source out.
        source_end = source_to_timeline_time(
            source_clip["to"], source_start=source_start, timeline_start=start, speed=rate,
        )
        end = min(end, source_end)
    origin = source_to_timeline_time(0, source_start=source_start, timeline_start=start, speed=rate)
    edges = [
        tuple(source_to_timeline_time(stored[item.word_id][edge], source_start=source_start,
                                     timeline_start=start, speed=rate)
              for edge in ("start", "end"))
        for item in phrase
    ]
    if any(not start <= onset < finish <= end for onset, finish in edges):
        raise TimingCompileError("word retime requires the whole phrase inside the played clip interval")
    projected = _ShotWordBinding(
        owner, field, declaration, exact.source, exact.document_id, exact.version,
        exact.content_digest, exact.word_ids, timeline_line=word.segment, first_audible=phrase[0].index == 0,
    )
    interval = edges[0][0], edges[-1][1]
    edge = interval[1] if moment.edge == "end" else interval[0]
    return _ResolvedWord(projected, edge, interval, (start, end, rate, origin))


class ShotWordRetimeInputs:
    """Share the projected transcript corpus across proofs for one bundle."""

    def __init__(self, bundle):
        self.bundle = bundle
        self._search_inputs = {}

    @cached_property
    def words(self):
        return _word_pool(self.bundle)

    def resolve(self, key, clip, field, text):
        """Return the exact binding, chosen word edge and target interval."""
        interval = self.interval(key, clip)
        word = self._resolve_word(key, field, text)
        return word.binding, word.edge, interval

    def word(self, key, field, text):
        try:
            return self._resolve_word(key, field, text)
        except (TimingCompileError, TimelineEditError, ArithmeticError, KeyError, TypeError, ValueError):
            return None

    def _resolve_word(self, key, field, text):
        _, shot, clip_id = key
        placement = _placement(self.bundle, shot)
        owner = (placement["occurrence_id"], shot, clip_id)
        return _exact_word(self.bundle, self.words, owner, field, text)

    def interval(self, key, clip):
        start = _start(_placement(self.bundle, key[1]), clip)
        return start, start + _duration(clip)

    def search_inputs(self, line):
        """Compare potential sources when an exact word cannot be resolved."""
        if line not in self._search_inputs:
            self._search_inputs[line] = self._audio_search_inputs(line)
        return self._search_inputs[line]

    def ambiguous_inputs(self, moment, search):
        """Identify every matching phrase when the retained search is complete."""
        try:
            words, captures = self.words
            require_retime_word_scope(self.bundle, captures, moment.line)
            hits = moments.find_words(moment, words)
            if len(hits) < 2:
                return None
            placements = sorted(self.bundle["placements"], key=_row_start)
            matches = []
            for phrase in hits:
                run = []
                for word in phrase:
                    if not word.word_id or word.media_id not in captures:
                        return None
                    occurrence = placements[word.occurrence]["occurrence_id"] if word.shot_id else None
                    source = occurrence, word.shot_id, word.clip_id
                    length, inputs = search.audio[source]
                    holder = (self.bundle["shots"][word.shot_id]["internal_timeline"]
                              if word.shot_id else self.bundle["parent"])
                    clip, = (row for row in _holder_clips(holder) if row.get("id") == word.clip_id)
                    source_in, source_out, rate = _source_window(clip)
                    clip_end = max(0.0, float(clip.get("at") or 0)) + (source_out - source_in) / rate
                    # An occurrence tail beyond the clip cannot affect this hit.
                    run.append((source, min(length, clip_end), inputs, word.word_id, word.segment,
                                word.index, word.start, word.end))
                matches.append(run)
            return matches, search.references, search.pins, search.captures
        except (TimingCompileError, TimelineEditError, ArithmeticError, KeyError, TypeError, ValueError):
            return None

    def _audio_search_inputs(self, line):
        bundle = self.bundle
        placements = sorted(bundle["placements"], key=_row_start)
        audio, media_ids = {}, set()
        for occurrence in _occurrences(bundle, "parent"):
            holder = occurrence["holder"]
            tracks = _holder_tracks(holder)
            assets = _registry_assets(holder)
            placement = placements[occurrence["occurrence"]] if occurrence["shot_id"] else {}
            for clip in _holder_clips(holder):
                clip_line = intent.line(clip) or str(clip.get("id") or "")
                if (line is not None and line != clip_line) or not _track_is_audio(tracks, clip):
                    continue
                media = _clip_media_id(holder, clip)
                if media:
                    media_ids.add(media)
                key = (placement.get("occurrence_id"), occurrence["shot_id"], clip.get("id"))
                asset = assets.get(clip.get("asset"))
                source_rows = (holder, clip, asset if isinstance(asset, Mapping) else {})
                declarations = intent._app(clip)
                audio[key] = occurrence["length"], (
                    occurrence["owner"], active(placement),
                    tuple(placement.get(name) for name in ("source_offset", "source_offset_ms", "speed",
                                                           "time_map", "timeMap")),
                    tuple(clip.get(name) for name in ("at", "at_ms", "from", "to", "speed", "hold", "duration", "track",
                                                      "asset", "asset_id", "media_id", "object_id")),
                    tuple(tuple(row.get(name) for name in ("source_window", "time_map", "timeMap"))
                          for row in source_rows),
                    tuple(declarations.get(name) for name in ("on", "until", "for", "until_film_end", "cut",
                                                              "sequence", "sequence_fit", "formulas", "orphan",
                                                              "gap_after_s", "fixed_start", "word_anchors")),
                    media, clip_line, intent.words(clip), active(clip),
                    tuple(active(track) for track in tracks if track.get("id") == clip.get("track")),
                )
        references = [row for row in reference_rows(bundle)
                      if row.get("mediaId") in media_ids or (media_ids and row.get("mediaId") is None)]
        identities = {row["documentId"] for row in references} | {
            transcript_document_id(bundle["project_id"], media, source)
            for media in media_ids for source in default_source_order()
        }
        pins = [pin for pin in retained_retime_pins(bundle.get("source_mapping", {}).get("_runtime_document"))
                if pin["document_id"] in identities]
        capture_rows = bundle.get("source_mapping", {}).get("pinned_documents", [])
        return _WordSearchInputs(audio, references, pins, [row["pin"] for row in capture_rows if row["pin"] in pins])


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
        before = baseline.word(key, field, text)
        after = candidate.word(key, field, text)
        if before is None or after is None or before.binding != after.binding:
            return None
        old_interval = baseline.interval(key, prior)
        new_interval = candidate.interval(key, clip)
        # An occurrence resize can leave an earlier clip stationary. Require
        # the same word interval and source playback in that case.
        if old_interval == new_interval and before != after:
            return None
        frame = 1 / _fps(candidate.bundle)
        if any(abs((new - old) - (new_word - old_word)) > frame
               for old, new in zip(old_interval, new_interval)
               for old_word, new_word in zip(before.interval, after.interval)):
            return None
    except (TimingCompileError, TimelineEditError, ArithmeticError, KeyError, TypeError, ValueError):
        return None
    return before.binding, after.binding, old_interval, new_interval


def confirm_shot_word_intervals(result, expected, fps):
    """Require that exact resolution preserves the stored start and end frames."""
    return all(owner in result.timings and all(
        quantize_time(result.timings[owner][edge], fps) == quantize_time(value, fps)
        for edge, value in zip(("start", "end"), interval)
    ) for owner, interval in expected.items())
