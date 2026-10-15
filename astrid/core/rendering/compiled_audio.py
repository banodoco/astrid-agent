"""Frozen derived audio transport. Digests bind data; runtime admission owns authority."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from astrid.core.rendering.contracts import CompiledAudio
from astrid.core.timeline import audio_curves
from astrid.core.timeline.audio_envelope import AudioEnvelopeError, compile_audio_values, validate


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def refuse(code: str, message: str):
    raise ValueError(f"{code}: {message}")


def _intent(clip, key):
    app = clip.get("app") or {}
    if not isinstance(app, Mapping) or not isinstance(app.get("astrid", {}), Mapping):
        refuse("AUDIO_CONTEXT_INVALID", "clip app namespace must be an object")
    return app.get("astrid", {}).get(key, app.get(key))


def _clips(config):
    clips = config.get("clips", [])
    if not isinstance(clips, list) or any(
        not isinstance(c, Mapping) or not isinstance(c.get("id"), str) or not c.get("id")
        for c in clips
    ):
        refuse("AUDIO_OCCURRENCE_INVALID", "projected clips require exact ids")
    if len({c["id"] for c in clips}) != len(clips):
        refuse("AUDIO_OCCURRENCE_DUPLICATE", "duplicate projected clip id")
    app = config.get("app") or {}
    if not isinstance(app, Mapping) or not isinstance(
        app.get("astrid_shot_composition", {}), Mapping
    ):
        refuse("AUDIO_CONTEXT_INVALID", "composition context must be an object")
    occurrences = app.get("astrid_shot_composition", {}).get("occurrences", [])
    if not isinstance(occurrences, list) or any(
        not isinstance(o, Mapping)
        or not isinstance(o.get("occurrence_id"), str)
        or not o["occurrence_id"]
        for o in occurrences
    ):
        refuse("AUDIO_OCCURRENCE_INVALID", "projected occurrences require exact ids")
    ids = [o["occurrence_id"] for o in occurrences]
    if len(set(ids)) != len(ids):
        refuse("AUDIO_OCCURRENCE_DUPLICATE", "duplicate projected occurrence")
    return clips


def audio_host(config, clip):
    kinds = {"audio", "vo", "voiceover", "voice", "music", "sfx"}
    tracks = {t["id"]: t.get("kind") for t in config.get("tracks", [])}
    return tracks.get(clip.get("track")) in kinds or clip.get("track") in kinds


def _source(config, authority):
    if (
        not isinstance(authority, Mapping)
        or not authority.get("head_event_id")
        or not authority.get("head_hash")
    ):
        refuse("AUDIO_CONTEXT_MISSING", "admitted revision/candidate authority is required")
    # Bind only stable source identity, never attempt paths or self-referential hashes.
    pin = {
        k: authority.get(k)
        for k in (
            "project_id",
            "timeline_id",
            "head_event_id",
            "head_hash",
            "authoring_preview",
            "expansion",
        )
    }
    return {
        "pin": pin,
        "word_inputs": [
            {"id": c["id"], "at": c.get("at", 0), "words": _intent(c, "words")}
            for c in _clips(config)
        ],
    }


def payload(config, authority, clips) -> CompiledAudio:
    return {
        "schema_version": 1,
        "source_context_digest": digest(_source(config, authority)),
        "timeline_config_digest": digest(config),
        "curve_registry_digest": digest(audio_curves.registry()),
        "clips": copy.deepcopy(clips),
    }


def compile_snapshot_audio(config: Mapping, authority: Mapping) -> CompiledAudio | None:
    """Compile once after occurrence projection, using pinned clip-relative word rows.

    Matches SDK words(): audio tracks only, clip at + word offset (already reflowed).
    No live checkout, transcript lookup or caller-authored digest is consulted.
    """
    clips = _clips(config)
    words = []
    ducking = any(_intent(clip, "duck") is not None for clip in clips)
    for clip in clips:
        if not ducking:
            break
        if not audio_host(config, clip):
            continue
        raw = _intent(clip, "words")
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise AudioEnvelopeError(f"{clip['id']}: pinned words must be an array", duck=True)
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise AudioEnvelopeError(f"{clip['id']}: invalid pinned word", duck=True)
            start, end = row[:2]
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
            ):
                raise AudioEnvelopeError(f"{clip['id']}: invalid pinned word timing", duck=True)
            words.append(
                SimpleNamespace(
                    start=round(float(clip.get("at", 0)) + start, 6),
                    end=round(float(clip.get("at", 0)) + end, 6),
                )
            )
    fps = config.get("output", {}).get("fps", config.get("fps", 30))
    compiled = {}
    for clip in clips:
        rule = _intent(clip, "duck")
        if not audio_host(config, clip):
            if rule is not None:
                raise AudioEnvelopeError(f"{clip['id']}: duck requires an audio host", duck=True)
            continue
        if rule is not None and not words:
            raise AudioEnvelopeError(
                f"{clip['id']}: pinned narration word context is missing", duck=True
            )
        audio = compile_audio_values(
            clip, fps=fps, words=words, clip_at=float(clip.get("at", 0)), duck_rule=rule
        )
        if audio is not None:
            compiled[clip["id"]] = audio
    return payload(config, authority, compiled) if compiled else None


def validate_payload(value):
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "schema_version",
            "source_context_digest",
            "timeline_config_digest",
            "curve_registry_digest",
            "clips",
        }
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        refuse("AUDIO_PAYLOAD_INVALID", "invalid compiled audio fields/version")
    if any(
        not isinstance(value[k], str) or not re.fullmatch(r"[0-9a-f]{64}", value[k])
        for k in ("source_context_digest", "timeline_config_digest", "curve_registry_digest")
    ):
        refuse("AUDIO_PAYLOAD_INVALID", "invalid binding digest")
    if not isinstance(value["clips"], Mapping):
        refuse("AUDIO_PAYLOAD_INVALID", "clips must be an object")
    for key, audio in value["clips"].items():
        if not isinstance(key, str) or not isinstance(audio, Mapping) or set(audio) != {"envelope"}:
            refuse("AUDIO_PAYLOAD_INVALID", "clip audio must contain only envelope")
        validate(audio["envelope"])
    return copy.deepcopy(dict(value))


def attach_compiled_audio(config, compiled_audio, authority, *, renderer_config=None):
    """Validate raw canonical binding, then attach only to detached renderer copies."""
    originals = _clips(config)
    result = copy.deepcopy(config if renderer_config is None else renderer_config)
    targets = _clips(result)
    if {c["id"] for c in targets} != {c["id"] for c in originals}:
        refuse("AUDIO_OCCURRENCE_MISMATCH", "renderer projection changed occurrence membership")
    if compiled_audio is None:
        if isinstance(authority, Mapping) and authority.get("compiled_audio_digest"):
            refuse("AUDIO_PAYLOAD_MISSING", "admitted compiled audio was dropped")
        if any(_intent(c, "duck") is not None for c in originals):
            raise AudioEnvelopeError("pinned duck has no compiled audio payload", duck=True)
        return result
    value = validate_payload(compiled_audio)
    expected = payload(config, authority, {})
    for name, code in (
        ("source_context_digest", "AUDIO_SOURCE_STALE"),
        ("timeline_config_digest", "AUDIO_CONFIG_STALE"),
        ("curve_registry_digest", "AUDIO_REGISTRY_STALE"),
    ):
        if value[name] != expected[name]:
            refuse(code, f"{name} does not match admitted render inputs")
    ids = {c["id"] for c in originals}
    if set(value["clips"]) - ids:
        refuse("AUDIO_OCCURRENCE_UNKNOWN", "compiled audio references unknown projected clips")
    required = {
        c["id"]
        for c in originals
        if _intent(c, "duck") is not None
        or (
            audio_host(config, c)
            and compile_audio_values(
                c,
                fps=config.get("output", {}).get("fps", config.get("fps", 30)),
                words=[],
                clip_at=0,
                duck_rule=None,
            )
            is not None
        )
    }
    if set(value["clips"]) != required:
        refuse("AUDIO_OCCURRENCE_MISMATCH", "compiled membership must exactly cover envelope hosts")
    if not authority.get("compiled_audio_digest"):
        refuse("AUDIO_CONTEXT_MISSING", "compiled audio requires its admitted payload identity")
    if authority["compiled_audio_digest"] != digest(value):
        refuse("AUDIO_PAYLOAD_STALE", "payload differs from admitted compiled audio")
    for clip in targets:
        if clip["id"] in value["clips"]:
            clip["audio"] = copy.deepcopy(value["clips"][clip["id"]])
    return result
