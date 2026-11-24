"""The one word projection: stored media transcripts mapped onto timeline occurrences.

A media transcript document (``astrid/sdk/media_transcript.py``) holds the words of one
media file in that file's own seconds, once per media and source. Every reader maps them
through the clip windows here, so trim, speed, nesting and repeated placements are applied
in one place. A clip whose media has no stored transcript falls back to its clip-local
``app.words`` so nothing is lost on timelines that were never transcribed.

Scopes:

* ``"parent"``: every audio clip as the parent shows it. Each placement of a shot repeats
  its clips at that placement's start, clipped to the placement length.
* ``"shot:<id>"``: one shot's own sub-timeline (owner start 0), clipped to the shot length.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from astrid.core.timeline.cuts import clip_duration
from astrid.sdk import timeline_intent as intent
from astrid.sdk.media_ids import canonical_digest
from astrid.sdk.media_transcript import IMPORT_SOURCE, NARRATION_SOURCE, transcript_document_id
from astrid.sdk.timeline._common import AUDIO_KINDS, PARENT_SHOT
from astrid.sdk.timeline.doc import _read_words, _row_start

TTS_SOURCE = "astrid.tts"
PARENT_LABEL = "parent"
PINNED_MODEL = "large-v3-turbo.pt"
_EPS = 1e-9


def default_source_order() -> tuple[str, ...]:
    """Whisper first, then TTS words, hand imports and converted narration."""
    from astrid.sdk.whisper_transcript import whisper_source_id

    return (whisper_source_id(PINNED_MODEL), TTS_SOURCE, IMPORT_SOURCE, NARRATION_SOURCE)


def reference_rows(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The timeline's ``transcriptReferences`` associations (``documentId``, and ``mediaId``/``sourceId``
    when recorded by ``timelines transcripts refresh``)."""
    config = (bundle.get("parent") or {}).get("config") or {}
    bundle_config = config.get("bundle") if isinstance(config, Mapping) else None
    refs = bundle_config.get("transcriptReferences") if isinstance(bundle_config, Mapping) else None
    associations = refs.get("associations") if isinstance(refs, Mapping) else None
    return [dict(row) for row in associations or []
            if isinstance(row, Mapping) and isinstance(row.get("documentId"), str)]


def transcript_references(bundle: Mapping[str, Any]) -> set[str]:
    """Document ids the timeline pins through ``transcriptReferences`` (per-media overrides)."""
    return {row["documentId"] for row in reference_rows(bundle)}


def _registry_assets(holder: Mapping[str, Any]) -> Mapping[str, Any]:
    registry = holder.get("registry") or (holder.get("config") or {}).get("registry") or {}
    assets = registry.get("assets") if isinstance(registry, Mapping) else None
    return assets if isinstance(assets, Mapping) else {}


def _clip_media_id(holder: Mapping[str, Any], clip: Mapping[str, Any]) -> str | None:
    if not clip.get("asset"):
        return None
    asset = _registry_assets(holder).get(clip.get("asset"))
    if not isinstance(asset, Mapping):
        return None
    media = asset.get("media_id") or asset.get("content_sha256")
    return media if isinstance(media, str) and media else None


def _track_is_audio(tracks: Sequence[Mapping[str, Any]], clip: Mapping[str, Any]) -> bool:
    track = str(clip.get("track") or "")
    kind = next((str(row.get("kind") or "").lower() for row in tracks
                 if isinstance(row, Mapping) and str(row.get("id")) == track), "")
    return kind in AUDIO_KINDS or track in AUDIO_KINDS


def _own_or_mirror(holder: Mapping[str, Any], key: str) -> list[Any]:
    """``holder[key]`` when it has rows, else ``holder.config[key]`` (the checkout's own-or-mirror rule)."""
    config = holder.get("config")
    own = holder.get(key)
    mirror = config.get(key) if isinstance(config, Mapping) else None
    chosen = own or mirror
    return list(chosen) if isinstance(chosen, list) else []


def _is_silent(clip: Mapping[str, Any]) -> bool:
    """A clip marked muted, or with volume 0 (top level or in its params): no speech is heard from it."""
    params = clip.get("params") if isinstance(clip.get("params"), Mapping) else {}
    if clip.get("muted") is True or params.get("muted") is True:
        return True
    for value in (clip.get("volume"), params.get("volume")):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
            return True
    return False


def _holder_clips(holder: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [c for c in _own_or_mirror(holder, "clips") if isinstance(c, Mapping)]


def _holder_tracks(holder: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [t for t in _own_or_mirror(holder, "tracks") if isinstance(t, Mapping)]


def _source_window(clip: Mapping[str, Any]) -> tuple[float, float, float]:
    """(source in, source out, speed) of a clip: source seconds are ``from`` .. ``to``."""
    src_in = float(clip.get("from") or 0)
    speed = float(clip.get("speed") or 1)
    if speed <= 0:
        raise ValueError(f"clip {clip.get('id')!r} has a non-positive speed")
    src_out = src_in + clip_duration(clip) * speed
    if clip.get("to") is not None:
        src_out = min(float(clip["to"]), src_out)
    return src_in, src_out, speed


def _document_words(content: Mapping[str, Any]) -> list[tuple[str | None, float, float, str]]:
    """Timed words of a media.transcript content, in its own seconds, sorted by start."""
    rows: list[tuple[str | None, float, float, str]] = []
    for segment in content.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        for word in segment.get("words") or []:
            if not isinstance(word, Mapping):
                continue
            start, end, text = word.get("start"), word.get("end"), word.get("text")
            if (isinstance(start, bool) or isinstance(end, bool)
                    or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                    or not isinstance(text, str) or not text.strip() or end < start):
                continue
            wid = word.get("id")
            rows.append((wid if isinstance(wid, str) else None, float(start), float(end), text))
    rows.sort(key=lambda row: (row[1], row[2]))
    return rows


def choose_document(media_docs: Mapping[str, Mapping[str, Any]], references: set[str],
                    sources: Sequence[str]) -> tuple[str, Mapping[str, Any]] | None:
    """The (source, document) a media uses: a pinned reference first, else source order."""
    for source, doc in media_docs.items():
        if doc.get("document_id") in references:
            return source, doc
    for source in sources:
        if source in media_docs:
            return source, media_docs[source]
    return None


def _occurrences(bundle: Mapping[str, Any], scope: str) -> list[dict[str, Any]]:
    """Each occurrence: which holder plays, at what owner start, for how long, as which shot.

    ``shot:<id>`` is clipped to the first placement's length: the placements of one shot share a
    length in practice. The parent scope clips each placement by its own length, so a shot whose
    placements differ shows its full words only where its own placement is long enough."""
    placements = sorted((r for r in bundle.get("placements") or [] if isinstance(r, Mapping)), key=_row_start)
    shots = bundle.get("shots") or {}
    if scope == "parent":
        found = [{"holder": bundle.get("parent") or {}, "shot_id": PARENT_SHOT, "owner": 0.0,
                  "length": math.inf, "occurrence": 0}]
        for index, row in enumerate(placements):
            sid = str(row.get("shot_id"))
            shot = shots.get(sid) or {}
            found.append({"holder": shot.get("internal_timeline") or {}, "shot_id": sid,
                          "owner": _row_start(row) / 1000.0,
                          "length": float(row.get("duration_ms") or 0) / 1000.0, "occurrence": index})
        return found
    if scope.startswith("shot:"):
        sid = scope[len("shot:"):]
        rows = [r for r in placements if str(r.get("shot_id")) == sid]
        if sid not in shots:
            raise ValueError(f"no shot {sid!r} in this timeline")
        length = float(rows[0].get("duration_ms") or 0) / 1000.0 if rows else math.inf
        holder = (shots.get(sid) or {}).get("internal_timeline") or {}
        return [{"holder": holder, "shot_id": sid, "owner": 0.0, "length": length, "occurrence": 0}]
    raise ValueError(f"unknown word scope {scope!r} (use 'parent' or 'shot:<id>')")


def project_words(bundle: Mapping[str, Any], transcripts: Mapping[str, Mapping[str, Mapping[str, Any]]],
                  scope: str = "parent", *, sources: Sequence[str] | None = None,
                  references: set[str] | None = None, media: str = "audio") -> list[dict[str, Any]]:
    """Every spoken word of a timeline in the given scope, one row per clip occurrence.

    ``transcripts`` maps media id -> source id -> ``{"document_id", "version", "content"}``.
    A word is kept when ``from <= t < to`` for its clip; its timeline time is
    ``owner_start + max(0, clip.at) + (t - from) / speed``. ``media="audio"`` takes clips on audio
    tracks; ``media="any"`` takes every clip with a managed media digest (a video clip with embedded
    audio included). Rows are sorted by timeline time and
    carry an ``address`` (``<line>:<k>``: the clip's line id, or its own id, and the word's
    index in that line) plus the stored ``word_id``, media, source, document version, clip,
    occurrence, shot, timeline time and source time.
    """
    order = tuple(sources) if sources is not None else default_source_order()
    refs = transcript_references(bundle) if references is None else references
    occurrences = _occurrences(bundle, scope)
    # Linked A/V (a video clip and its audio clip on one media): the audio track carries the speech,
    # so a non-audio clip of that media adds nothing in "any" mode.
    audio_media = {_clip_media_id(occ["holder"], clip) for occ in occurrences
                   for clip in _holder_clips(occ["holder"])
                   if _track_is_audio(_holder_tracks(occ["holder"]), clip)}
    rows: list[dict[str, Any]] = []
    for occ in occurrences:
        holder, owner, length = occ["holder"], occ["owner"], occ["length"]
        tracks = _holder_tracks(holder)
        for clip in _holder_clips(holder):
            clip_media = _clip_media_id(holder, clip)
            audio_track = _track_is_audio(tracks, clip)
            if media == "audio" and not audio_track:
                continue
            if media == "any":
                if clip_media is None and not intent.words(clip):
                    continue
                if _is_silent(clip):
                    continue  # muted or volume 0: nothing is heard from it
                if not audio_track and clip_media in audio_media:
                    continue  # its audio track is the one that carries the speech
            segment = intent.line(clip) or str(clip.get("id") or "")
            clip_at = max(0.0, float(clip.get("at") or 0))  # a clip never starts before its placement
            chosen = choose_document(transcripts.get(clip_media) or {}, refs, order) if clip_media else None
            base = {"clip_id": str(clip.get("id") or ""), "shot_id": occ["shot_id"],
                    "occurrence": occ["occurrence"], "segment": segment}
            if chosen is not None:
                source, doc = chosen
                src_in, src_out, speed = _source_window(clip)
                for word_id, s, e, text in _document_words(doc["content"]):
                    if not (src_in - _EPS <= s < src_out - _EPS):
                        continue
                    t0 = owner + clip_at + (s - src_in) / speed
                    if t0 >= owner + length - _EPS:
                        continue
                    t1 = min(owner + clip_at + (min(e, src_out) - src_in) / speed, owner + length)
                    rows.append({**base, "word_id": word_id, "text": text, "media_id": clip_media,
                                 "source": source, "version": doc.get("version"),
                                 "document_id": doc.get("document_id"),
                                 "timeline_start": t0, "timeline_end": t1,
                                 "source_start": s, "source_end": e})
            else:
                # No stored transcript used for this clip's media: keep its clip-local words.
                for s, e, text in _read_words({"words": intent.words(clip)}, relative=True):
                    t0 = owner + clip_at + s
                    if t0 >= owner + length - _EPS:
                        continue
                    rows.append({**base, "word_id": None, "text": text, "media_id": clip_media,
                                 "source": "clip", "version": None, "document_id": None,
                                 "timeline_start": t0, "timeline_end": owner + clip_at + e,
                                 "source_start": s, "source_end": e})
    rows.sort(key=lambda row: (row["timeline_start"], row["segment"], row["clip_id"], row["source_start"]))
    counters: dict[str, int] = {}
    for row in rows:
        segment = row["segment"]  # the clip's line id, or its own id when it has none (the address timelines words uses)
        k = counters.get(segment, 0)
        counters[segment] = k + 1
        row["index"] = k
        row["address"] = f"{segment}:{k}"
    return rows


def timeline_media_ids(bundle: Mapping[str, Any], *, any_media: bool = False,
                       line: str | None = None) -> list[str]:
    """Media digests of the timeline's audio clips (every clip with a digest when ``any_media``),
    in the parent and every shot's sub-timeline, in order. A line restricts the
    search to the same line ID used by the word projection."""
    media_ids: list[str] = []
    for occ in _occurrences(bundle, "parent"):
        tracks = _holder_tracks(occ["holder"])
        for clip in _holder_clips(occ["holder"]):
            if line is not None and line != (intent.line(clip) or str(clip.get("id") or "")):
                continue
            media = _clip_media_id(occ["holder"], clip)
            if media and (any_media or _track_is_audio(tracks, clip)) and media not in media_ids:
                media_ids.append(media)
    return media_ids


def load_timeline_transcripts(client: Any, project: str, bundle: Mapping[str, Any], *,
                              sources: Sequence[str] | None = None, any_media: bool = False,
                              issues: list[str] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """Read the stored transcripts a timeline can use: each media's sources in order, plus every document
    a ``transcriptReferences`` pin names (any source). A pin that cannot be loaded is appended to
    ``issues`` instead of being dropped silently."""
    from astrid.sdk import media_transcript

    order = tuple(sources) if sources is not None else default_source_order()
    refs = reference_rows(bundle)
    project_id = bundle.get("project_id")
    wanted: list[tuple[str, str]] = [(media, source) for media in timeline_media_ids(bundle, any_media=any_media)
                                     for source in order]
    for ref in refs:
        media, source = ref.get("mediaId"), ref.get("sourceId")
        if isinstance(media, str) and isinstance(source, str):
            wanted.append((media, source))
        elif issues is not None:
            issues.append(f"pinned document {ref['documentId']} records no media and source; "
                          "re-record it with timelines transcripts refresh --use-document")
    loaded: dict[str, dict[str, dict[str, Any]]] = {}
    for media, source in dict.fromkeys(wanted):
        if source in loaded.get(media, {}):
            continue
        digest = canonical_digest(media)
        if digest is None:  # a registry id that is not a digest names no stored transcript
            continue
        data = media_transcript.read_transcript(client, project, digest, source)
        if not isinstance(data, Mapping) or not isinstance(data.get("content"), Mapping):
            continue
        document_id = data.get("document_id") or (
            transcript_document_id(str(project_id), media, source) if project_id else None)
        version = data.get("version")
        loaded.setdefault(media, {})[source] = {
            "document_id": document_id, "version": version if type(version) is int else None,
            "content": data["content"]}
    if issues is not None:
        for ref in refs:
            media, source = ref.get("mediaId"), ref.get("sourceId")
            if not (isinstance(media, str) and isinstance(source, str)):
                continue
            doc = loaded.get(media, {}).get(source)
            if doc is None:
                issues.append(f"pinned to {source} for {media} ({ref['documentId']}): that transcript is not stored")
            elif doc.get("document_id") not in (None, ref["documentId"]):
                issues.append(f"pinned document {ref['documentId']} for {media} is not the stored {source} document")
    return loaded


def pin_notes(bundle: Mapping[str, Any], transcripts: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> list[str]:
    """A pinned source that a higher-ranked stored source (earlier in the default order) would beat."""
    order = default_source_order()
    rank = {source: index for index, source in enumerate(order)}
    notes = []
    for ref in reference_rows(bundle):
        media, source = ref.get("mediaId"), ref.get("sourceId")
        if not (isinstance(media, str) and isinstance(source, str)):
            continue
        docs = transcripts.get(media) or {}
        higher = [other for other in docs if other != source
                  and rank.get(other, len(order)) < rank.get(source, len(order))]
        if higher:
            notes.append(f"{media}: pinned to {source}; {', '.join(higher)} is available")
    return notes


def frozen_speech_words(client: Any, project: str, config: Mapping[str, Any],
                        registry: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Every timed word of a flattened (render) timeline, frozen with its document id and version:
    stored transcripts first (any clip with a media digest), clip-local words where no transcript is stored.
    Returns (rows in parent seconds, issues)."""
    from astrid.sdk.media_transcript import _project_id

    bundle = {"project_id": _project_id(client, project), "placements": [],
              "parent": {"clips": [c for c in config.get("clips") or [] if isinstance(c, Mapping)],
                         "tracks": [t for t in config.get("tracks") or [] if isinstance(t, Mapping)],
                         "registry": dict(registry or {})},
              "shots": {}}
    issues: list[str] = []
    transcripts = load_timeline_transcripts(client, project, bundle, any_media=True, issues=issues)
    rows = project_words(bundle, transcripts, "parent", media="any")
    frozen = [{"address": row["address"], "text": row["text"],
               "start": round(row["timeline_start"], 6), "end": round(row["timeline_end"], 6),
               "clip_id": row["clip_id"], "word_id": row.get("word_id"), "media_id": row.get("media_id"),
               "source": row.get("source"), "version": row.get("version"),
               "document_id": row.get("document_id")} for row in rows]
    return frozen, issues


def as_words(rows: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Project rows as the ``Word`` records the timeline API returns."""
    from astrid.sdk.timeline.model import Word

    return [Word(str(row["address"]), str(row["text"]), round(row["timeline_start"], 6),
                 round(row["timeline_end"], 6), str(row["segment"] or row["clip_id"]), int(row["index"]),
                 row["clip_id"], row["shot_id"], word_id=row.get("word_id"), media_id=row.get("media_id"),
                 source=row.get("source"), version=row.get("version"), occurrence=row.get("occurrence", 0))
            for row in rows]
