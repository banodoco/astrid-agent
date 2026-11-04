"""Attempt-local cache for measured filmstrip audio analysis."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _audio_cache_path(parent: Path, render_digest: str, settings: object) -> Path:
    key = hashlib.sha256(json.dumps(
        {"render_digest": render_digest, "settings": settings},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    return parent / ".audio-analysis-cache" / f"{key}.json"


def _cached_audio(parent: Path, render_digest: str, settings: object) -> dict | None:
    path = _audio_cache_path(parent, render_digest, settings)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("render_digest") != render_digest:
        return None
    return value


def _store_audio_cache(parent: Path, render_digest: str, settings: object, value: dict) -> None:
    path = _audio_cache_path(parent, render_digest, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    entries = sorted(path.parent.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in entries[256:]:
        stale.unlink(missing_ok=True)
