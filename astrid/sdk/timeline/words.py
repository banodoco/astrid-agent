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
from astrid.sdk.media_transcript import IMPORT_SOURCE, NARRATION_SOURCE, transcript_document_id
from astrid.sdk.timeline._common import AUDIO_KINDS, PARENT_SHOT
from astrid.sdk.timeline.doc import _read_words, _row_start

TTS_SOURCE = "astrid.tts"
PINNED_MODEL = "large-v3-turbo.pt"
_EPS = 1e-9


def default_source_order() -> tuple[str, ...]:
    """Whisper first, then TTS words, hand imports and converted narration."""
    from astrid.sdk.whisper_transcript import whisper_source_id

    return (whisper_source_id(PINNED_MODEL), TTS_SOURCE, IMPORT_SOURCE, NARRATION_SOURCE)


def transcript_references(bundle: Mapping[str, Any]) -> set[str]:
    """Document ids the timeline pins through ``transcriptReferences`` (per-media overrides)."""
    config = (bundle.get("parent") or {}).get("config") or {}
    bundle_config = config.get("bundle") if isinstance(config, Mapping) else None
    refs = bundle_config.get("transcriptReferences") if isinstance(bundle_config, Mapping) else None
    associations = refs.get("associations") if isinstance(refs, Mapping) else None
    return {str(row["documentId"]) for row in associations or []
            if isinstance(row, Mapping) and isinstance(row.get("documentId"), str)}


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


def _holder_clips(holder: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    clips = holder.get("clips")
    if not isinstance(clips, list):
        clips = (holder.get("config") or {}).get("clips") if isinstance(holder.get("config"), Mapping) else None
    return [c for c in clips or [] if isinstance(c, Mapping)]


def _holder_tracks(holder: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tracks = holder.get("tracks")
    if not isinstance(tracks, list):
        tracks = (holder.get("config") or {}).get("tracks") if isinstance(holder.get("config"), Mapping) else None
    return [t for t in tracks or [] if isinstance(t, Mapping)]


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
    """Each occurrence: which holder plays, at what owner start, for how long, as which shot."""
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
                  references: set[str] | None = None) -> list[dict[str, Any]]:
    """Every spoken word of a timeline in the given scope, one row per clip occurrence.

    ``transcripts`` maps media id -> source id -> ``{"document_id", "version", "content"}``.
    A word is kept when ``from <= t < to`` for its clip; its timeline time is
    ``owner_start + clip.at + (t - from) / speed``. Rows are sorted by timeline time and
    carry an ``address`` (``<line>:<k>`` where the clip has a line segment id, else
    ``<clip>:<k>``) plus the stored ``word_id``, media, source, document version, clip,
    occurrence, shot, timeline time and source time.
    """
    order = tuple(sources) if sources is not None else default_source_order()
    refs = transcript_references(bundle) if references is None else references
    rows: list[dict[str, Any]] = []
    for occ in _occurrences(bundle, scope):
        holder, owner, length = occ["holder"], occ["owner"], occ["length"]
        tracks = _holder_tracks(holder)
        for clip in _holder_clips(holder):
            if not _track_is_audio(tracks, clip):
                continue
            media = _clip_media_id(holder, clip)
            segment = intent.line(clip) or str(clip.get("id") or "")
            clip_at = float(clip.get("at") or 0)
            chosen = choose_document(transcripts.get(media) or {}, refs, order) if media else None
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
                    rows.append({**base, "word_id": word_id, "text": text, "media_id": media,
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
                    rows.append({**base, "word_id": None, "text": text, "media_id": media,
                                 "source": "clip", "version": None, "document_id": None,
                                 "timeline_start": t0, "timeline_end": owner + clip_at + e,
                                 "source_start": s, "source_end": e})
    rows.sort(key=lambda row: (row["timeline_start"], row["segment"], row["clip_id"], row["source_start"]))
    counters: dict[str, int] = {}
    for row in rows:
        segment = row["segment"] or row["clip_id"]
        k = counters.get(segment, 0)
        counters[segment] = k + 1
        row["index"] = k
        row["address"] = f"{segment}:{k}" if row["segment"] else (row["word_id"] or f"{row['clip_id']}:{k}")
    return rows


def timeline_media_ids(bundle: Mapping[str, Any]) -> list[str]:
    """Media digests of every audio clip in the parent and every shot's sub-timeline, in order."""
    media_ids: list[str] = []
    for occ in _occurrences(bundle, "parent"):
        tracks = _holder_tracks(occ["holder"])
        for clip in _holder_clips(occ["holder"]):
            media = _clip_media_id(occ["holder"], clip)
            if media and _track_is_audio(tracks, clip) and media not in media_ids:
                media_ids.append(media)
    return media_ids


def load_timeline_transcripts(client: Any, project: str, bundle: Mapping[str, Any], *,
                              sources: Sequence[str] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """Read every stored transcript this timeline's audio clips could use (one read per media and source)."""
    from astrid.sdk import media_transcript

    order = tuple(sources) if sources is not None else default_source_order()
    media_ids = timeline_media_ids(bundle)
    project_id = bundle.get("project_id")
    loaded: dict[str, dict[str, dict[str, Any]]] = {}
    for media in media_ids:
        for source in order:
            data = media_transcript.read_transcript(client, project, media, source)
            if not isinstance(data, Mapping) or not isinstance(data.get("content"), Mapping):
                continue
            document_id = data.get("document_id") or (
                transcript_document_id(str(project_id), media, source) if project_id else None)
            version = data.get("version")
            loaded.setdefault(media, {})[source] = {
                "document_id": document_id, "version": version if type(version) is int else None,
                "content": data["content"]}
    return loaded


def as_words(rows: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Project rows as the ``Word`` records the timeline API returns."""
    from astrid.sdk.timeline.model import Word

    return [Word(str(row["address"]), str(row["text"]), round(row["timeline_start"], 6),
                 round(row["timeline_end"], 6), str(row["segment"] or row["clip_id"]), int(row["index"]),
                 row["clip_id"], row["shot_id"], word_id=row.get("word_id"), media_id=row.get("media_id"),
                 source=row.get("source"), version=row.get("version"), occurrence=row.get("occurrence", 0))
            for row in rows]
