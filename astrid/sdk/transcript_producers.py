"""Producers that write the one media transcript store.

A media file has one stored transcript document per source (``media.transcript/v1``). Three producers
write it besides Whisper-through-ComfyUI itself:

* ``run_transcription``: the whole ``timelines transcripts run`` path for one media (GPU work), shared with
  ``astrid generate ... --transcribe`` so both do exactly the same thing;
* ``import_tts_words``: the word timings a speech generation already measured (``speech-words.json``), stored
  for the generated take under source ``astrid.tts``;
* ``transcribe_generated``: ``--transcribe`` after a completed generation whose output has audio.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from astrid.sdk.media_transcript import import_transcript_words

TTS_SOURCE = "astrid.tts"
TTS_SEGMENT = "speech"
_DIGEST = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_PLACEMENT_OPTIONS = ("placement", "engine", "execution", "align", "recovery_path")


def run_transcription(client: Any, project: str, media_id: str, *, consent: Sequence[str] = (),
                      replace: bool = False, expected_version: int | None = None,
                      language: str | None = None, prompt: str | None = None,
                      **options: Any) -> tuple[str, str, str]:
    """Transcribe one project media on the transcription workflow and store the result.

    Returns ``(status, media digest, run id)``. ``options`` are the placement controls
    (``placement``, ``engine``, ``execution``, ``align``, ``recovery_path``); only those that are set are used.
    ``language`` and ``prompt`` are optional hints for the model; left out, the workflow decides (auto, none).
    """
    from astrid.sdk.generation import generate
    from astrid.sdk.media_handles import resolve_media_handle
    from astrid.sdk.transcript_run import import_transcription_run, transcription_run_id

    descriptor, _ = resolve_media_handle(client, project, media_id)
    digest = descriptor["digest"]
    raw = client.media.read_bytes(digest)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("media bytes do not match their digest")
    hints = {name: value for name, value in (("language", language), ("prompt", prompt)) if value}
    with tempfile.TemporaryDirectory(prefix="astrid-transcription-") as tmp:
        path = Path(tmp) / descriptor["filename"]
        path.write_bytes(raw)
        chosen = {name: options[name] for name in _PLACEMENT_OPTIONS if options.get(name) is not None}
        result = generate.audio(entry="transcription", mode="transcribe", audio_ref=str(path),
                                project=project, client=client, consent=tuple(consent), **hints, **chosen)
    if getattr(result, "ok", True) is False:
        raise ValueError(f"transcription failed: {getattr(result, 'error', None)}")
    value = result.to_dict() if hasattr(result, "to_dict") else result
    run_id = transcription_run_id(client, value)
    status = import_transcription_run(client, project, digest, run_id, replace=replace,
                                      expected_version=expected_version, **hints)
    return status, digest, run_id


def _digest(row: Mapping[str, Any]) -> str | None:
    for field in ("sha256", "digest", "content_hash", "object_id"):
        match = _DIGEST.fullmatch(str(row.get(field) or "").lower())
        if match:
            return "sha256:" + match.group(1)
    return None


def _manifest_rows(client: Any, payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every output row (any role) the generation's manifest names."""
    manifest = payload.get("manifest")
    outputs = manifest.get("outputs") if isinstance(manifest, Mapping) else None
    if not isinstance(outputs, list) and isinstance(payload.get("manifest_ref"), str):
        digest = _DIGEST.fullmatch(payload["manifest_ref"].lower())
        if digest:
            sealed = json.loads(client.media.read_bytes("sha256:" + digest.group(1)))
            outputs = sealed.get("outputs") if isinstance(sealed, Mapping) else None
    return [row for row in outputs or [] if isinstance(row, Mapping)]


def _read_verified(client: Any, digest: str) -> bytes:
    raw = client.media.read_bytes(digest)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"object {digest} does not match its digest")
    return raw


def import_tts_words(client: Any, project: str, payload: Any) -> dict[str, Any] | None:
    """Store the measured word timings of a speech generation as the take's transcript (source ``astrid.tts``).

    ``payload`` is a completed generation result. Returns ``{"media_id", "status"}`` when it carries a
    ``speech-words.json`` for its audio result, else None. The words belong to the audio they were measured
    on: a ``speech-words.json`` whose ``audio_sha256`` is not that audio is refused.
    """
    if not isinstance(payload, Mapping):
        return None
    rows = _manifest_rows(client, payload)
    words_row = next((row for row in rows if row.get("name") == "speech_words"
                      or str(row.get("path") or "").endswith("speech-words.json")), None)
    audio = next((row for row in rows if row.get("role", "result") == "result"
                  and str(row.get("media_type") or "").startswith("audio/")), None)
    if words_row is None or audio is None:
        return None
    words_digest, audio_digest = _digest(words_row), _digest(audio)
    if words_digest is None or audio_digest is None:
        return None
    document = json.loads(_read_verified(client, words_digest))
    if not isinstance(document, Mapping) or not isinstance(document.get("words"), list):
        raise ValueError("speech-words.json has no words list")
    if "sha256:" + str(document.get("audio_sha256") or "").removeprefix("sha256:") != audio_digest:
        raise ValueError("speech-words.json was measured on different audio than this take")
    manifest = payload.get("manifest")
    text = manifest.get("text") if isinstance(manifest, Mapping) else None
    status = import_transcript_words(client, project, audio_digest, document["words"], segment_id=TTS_SEGMENT,
                                     text=text if isinstance(text, str) and text.strip() else None,
                                     source=TTS_SOURCE)
    return {"media_id": audio_digest, "status": status}


def audio_outputs(client: Any, payload: Any) -> list[dict[str, Any]]:
    """The result outputs of a delivered generation that carry audio: ``{"media_id", "path"}`` rows.

    Audio files count; a video counts when its local copy has an audio stream."""
    if not isinstance(payload, Mapping):
        return []
    rows = [row for row in _manifest_rows(client, payload) if row.get("role", "result") == "result"]
    paths = payload.get("image_paths") or []
    found: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        digest, kind = _digest(row), str(row.get("media_type") or "")
        path = paths[index] if len(paths) == len(rows) else None
        if digest is None:
            continue
        if kind.startswith("audio/"):
            has_audio = True
        elif kind.startswith("video/") and path:
            from astrid.core.media import ffprobe_metadata

            has_audio = bool(ffprobe_metadata(Path(path)).has_audio_stream)
        else:
            has_audio = False
        if has_audio and all(item["media_id"] != digest for item in found):
            found.append({"media_id": digest, "path": path})
    return found


def transcribe_generated(client: Any, project: str, payload: Any, **options: Any) -> list[dict[str, Any]]:
    """``--transcribe``: run the transcription workflow for each audio-bearing output of a completed generation.

    GPU work, the same path as ``timelines transcripts run``. One row per output:
    ``{"media_id", "status", "run_id"}``, or ``{"media_id", "status": "failed", "error"}`` (a failed
    transcription never undoes the generation that already succeeded)."""
    report: list[dict[str, Any]] = []
    for output in audio_outputs(client, payload):
        try:
            status, _digest_value, run_id = run_transcription(client, project, output["media_id"], **options)
            report.append({"media_id": output["media_id"], "status": status, "run_id": run_id})
        except Exception as exc:  # noqa: BLE001 - the generated media exists; say what failed, keep going
            report.append({"media_id": output["media_id"], "status": "failed", "error": str(exc)})
    return report


__all__ = ["TTS_SOURCE", "audio_outputs", "import_tts_words", "run_transcription", "transcribe_generated"]
