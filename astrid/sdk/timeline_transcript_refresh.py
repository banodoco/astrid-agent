"""Plan a transcript refresh: which transcript each audio media uses, what is missing, what a refresh would touch.

Read only. The CLI (``timelines transcripts refresh``) runs the missing transcriptions and records the
chosen documents; this module decides what those are and reports the problems a refresh cannot fix
by itself: word anchors pinned to an old document version, and clip windows that cut through a word.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from astrid.core.timeline.bundle import TIMELINE_BUNDLE_SCHEMA_VERSION
from astrid.sdk.timeline import words as W

REFERENCES_SCHEMA = "media.transcript.references/v1"
_EPS = 1e-6


def _holders(bundle: Mapping[str, Any], shot: str | None) -> list[tuple[str, Mapping[str, Any]]]:
    if shot is not None:
        row = (bundle.get("shots") or {}).get(shot)
        if not isinstance(row, Mapping):
            raise ValueError(f"no shot {shot!r} in this timeline")
        return [(shot, row.get("internal_timeline") or {})]
    found = [(W.PARENT_LABEL, bundle.get("parent") or {})]
    for sid, row in (bundle.get("shots") or {}).items():
        found.append((str(sid), (row or {}).get("internal_timeline") or {}))
    return found


def inventory(bundle: Mapping[str, Any], transcripts: Mapping[str, Mapping[str, Mapping[str, Any]]], *,
              shot: str | None = None, sources: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """One row per audio-bearing media in the parent and child timelines (or one shot's child timeline)."""
    order = tuple(sources) if sources is not None else W.default_source_order()
    refs = W.transcript_references(bundle)
    whisper = W.default_source_order()[0]
    media_rows: dict[str, dict[str, Any]] = {}
    for label, holder in _holders(bundle, shot):
        tracks = W._holder_tracks(holder)
        for clip in W._holder_clips(holder):
            media = W._clip_media_id(holder, clip)
            if media is None or not W._track_is_audio(tracks, clip):
                continue
            row = media_rows.setdefault(media, {"media_id": media, "holders": [], "clips": []})
            if label not in row["holders"]:
                row["holders"].append(label)
            row["clips"].append(f"{label}/{clip.get('id')}")
    rows = []
    for media, row in sorted(media_rows.items()):
        chosen = W.choose_document(transcripts.get(media) or {}, refs, order)
        if chosen is None:
            status, source, version = "missing", None, None
        else:
            source, doc = chosen
            status = "whisper" if source == whisper else f"other source only ({source})"
            version = doc.get("version")
        rows.append({**row, "status": status, "source": source, "version": version,
                     "pinned": bool(chosen and chosen[1].get("document_id") in refs)})
    return rows


def cut_words(bundle: Mapping[str, Any], transcripts: Mapping[str, Mapping[str, Mapping[str, Any]]], *,
              shot: str | None = None, sources: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Clip windows whose in or out point falls strictly inside a stored word."""
    order = tuple(sources) if sources is not None else W.default_source_order()
    refs = W.transcript_references(bundle)
    found: list[dict[str, Any]] = []
    for label, holder in _holders(bundle, shot):
        tracks = W._holder_tracks(holder)
        for clip in W._holder_clips(holder):
            media = W._clip_media_id(holder, clip)
            if media is None or not W._track_is_audio(tracks, clip):
                continue
            chosen = W.choose_document(transcripts.get(media) or {}, refs, order)
            if chosen is None:
                continue
            src_in, src_out, _speed = W._source_window(clip)
            for word_id, start, end, text in W._document_words(chosen[1]["content"]):
                for edge, point in (("in", src_in), ("out", src_out)):
                    if start + _EPS < point < end - _EPS:
                        found.append({"holder": label, "clip_id": str(clip.get("id")), "media_id": media,
                                      "word_id": word_id, "text": text, "edge": edge,
                                      "point": point, "word_start": start, "word_end": end})
    return found


def stale_anchors(bundle: Mapping[str, Any], transcripts: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Word anchors (parent clips) whose pinned document version is no longer the stored version."""
    current: dict[str, int | None] = {}
    for docs in transcripts.values():
        for doc in docs.values():
            if doc.get("document_id"):
                current[str(doc["document_id"])] = doc.get("version")
    found: list[dict[str, Any]] = []
    for clip in (bundle.get("parent") or {}).get("clips") or []:
        if not isinstance(clip, Mapping):
            continue
        app = clip.get("app")
        astrid = app.get("astrid") if isinstance(app, Mapping) else None
        anchors = astrid.get("word_anchors") if isinstance(astrid, Mapping) else None
        for field, choice in (anchors or {}).items():
            pin = choice.get("document") if isinstance(choice, Mapping) else None
            if not isinstance(pin, Mapping):
                continue
            doc_id, pinned = pin.get("document_id"), pin.get("version")
            if doc_id not in current:
                found.append({"clip_id": str(clip.get("id")), "field": str(field), "document_id": doc_id,
                              "pinned_version": pinned, "current_version": None,
                              "reason": "pinned document is not in the store"})
            elif current[doc_id] != pinned:
                found.append({"clip_id": str(clip.get("id")), "field": str(field), "document_id": doc_id,
                              "pinned_version": pinned, "current_version": current[doc_id],
                              "reason": "pinned to an older document version"})
    return found


class AssetResolutionError(ValueError):
    """A transcript reference cannot be given an editor-valid ``assetId``."""


def _identity(asset: Any) -> Any:
    """The media identity of a registry asset (as ``words._clip_media_id`` and the editor)."""
    return (asset.get("media_id") or asset.get("content_sha256")) if isinstance(asset, Mapping) else None


def resolve_asset_id(bundle: Mapping[str, Any], media: str) -> str | None:
    """The project-registry asset id whose media identity is ``media``, or None when there is none.

    Raises ``AssetResolutionError`` when two registry keys carry the media (ambiguous), or when the key
    is also used for a different media in the parent or another shot (the editor's parent registry wins
    on a key clash, so the key would point at the wrong asset)."""
    holders = [(label, W._registry_assets(holder)) for label, holder in _holders(bundle, None)]
    keys = sorted({str(key) for _label, assets in holders for key, asset in assets.items()
                   if _identity(asset) == media})
    if not keys:
        return None
    if len(keys) > 1:
        raise AssetResolutionError(f"media {media} is carried by more than one registry asset "
                                   f"({', '.join(repr(k) for k in keys)}): the asset is ambiguous")
    key = keys[0]
    for label, assets in holders:
        if key in assets and _identity(assets[key]) != media:
            raise AssetResolutionError(f"asset key {key!r} for media {media} is also used in {label!r} for a "
                                       "different media: the editor would resolve it to the wrong asset")
    return key


def with_reference(bundle: Mapping[str, Any], media: str, media_document_ids: set[str],
                   document_id: str, source: str, asset_id: str | None = None) -> list[dict[str, Any]]:
    """Associations after recording one media's explicit choice: that media's earlier pins are
    replaced; every other media's pin is kept. The row names its media and source so it can be
    loaded again (see ``words.load_timeline_transcripts``), and its ``assetId`` (the editor's key)
    when given."""
    kept = [row for row in W.reference_rows(bundle)
            if row.get("mediaId") != media and row["documentId"] not in media_document_ids]
    row: dict[str, Any] = {"documentId": document_id, "mediaId": media, "sourceId": source}
    if asset_id is not None:
        row = {"assetId": asset_id, **row}
    kept.append(row)
    return kept


def backfill_asset_ids(bundle: Mapping[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows written before ``assetId`` existed get it from the registry when their media resolves.
    A row whose media is gone from the registry fails closed, naming the row."""
    out = []
    for row in rows:
        if "assetId" not in row:
            found = resolve_asset_id(bundle, row["mediaId"]) if isinstance(row.get("mediaId"), str) else None
            if found is None:
                raise AssetResolutionError(
                    f"existing transcript reference (documentId {row.get('documentId')!r}, mediaId "
                    f"{row.get('mediaId')!r}) has no assetId and its media is not in the project registry: "
                    "remove the row or restore its media, then run refresh again")
            row = {"assetId": found, **row}
        out.append(row)
    return out


def complete_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Give a ``config.bundle`` envelope the header the editor requires (``schema_version``, ``itemsBySchemaRef``)."""
    envelope.setdefault("schema_version", TIMELINE_BUNDLE_SCHEMA_VERSION)
    envelope.setdefault("itemsBySchemaRef", {})
    return envelope


def record_choice(bundle: dict[str, Any], media: str, media_document_ids: set[str], document_id: str,
                  source: str, asset_id: str) -> dict[str, Any]:
    """Record one media's explicit choice in ``bundle`` (in place) and return its completed ``config.bundle`` envelope."""
    envelope = bundle.setdefault("parent", {}).setdefault("config", {}).setdefault("bundle", {})
    refs = envelope.setdefault("transcriptReferences", {"schema": REFERENCES_SCHEMA})
    refs["schema"] = REFERENCES_SCHEMA
    rows = with_reference(bundle, media, media_document_ids, document_id, source, asset_id)
    refs["associations"] = backfill_asset_ids(bundle, rows)
    return complete_envelope(envelope)
