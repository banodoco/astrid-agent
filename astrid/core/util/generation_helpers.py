"""Small shared helpers used by generation provider adapters and pack actions.

These helpers deliberately preserve each caller's domain payload and result
shape. They only centralize credential lookup, bounded output persistence, and
atomic JSON journal writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from astrid.core.foundation.atomic_io import write_json_atomic
from astrid.core.util.credentials_scope import CredentialsScope
from astrid.core.util.http import HttpClient

DEFAULT_OUTPUT_MAX_BYTES = 512 * 1024 * 1024
_OUTPUT_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov",
    ".wav", ".mp3", ".flac", ".m4a",
}


def load_provider_key(
    provider: str, *, env_file: Path | None = None, explicit: str | None = None
) -> str:
    """Load a provider key using Astrid's credential-store precedence."""
    return CredentialsScope.get_local(
        provider, env_file=env_file, explicit=explicit
    )


def output_suffix(url: str, *, fallback: str = ".png") -> str:
    """Return a normalized, allowlisted suffix from a URL path or fallback."""
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in _OUTPUT_SUFFIXES:
        return suffix
    normalized = fallback.lower()
    if not normalized.startswith("."):
        normalized = "." + normalized
    if normalized not in _OUTPUT_SUFFIXES:
        raise ValueError(f"unsupported output suffix: {fallback!r}")
    return normalized


def unique_output_path(path: Path) -> Path:
    """Choose the first unused deterministic numbered output path."""
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def download_output(
    client: HttpClient,
    url: str,
    destination: Path,
    *,
    timeout: int,
    max_bytes: int = DEFAULT_OUTPUT_MAX_BYTES,
    fallback_suffix: str = ".png",
    unique: bool = False,
    validate: Callable[[bytes], object] | None = None,
    infer_suffix: bool = True,
) -> tuple[Path, bytes]:
    """Download one bounded output and atomically persist it with a stable suffix.

    A caller-supplied destination suffix is respected. Otherwise the URL suffix
    is used when recognized, with the media-specific fallback supplied by the
    caller. ``unique=True`` retains the backend's numbered-name collision policy.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    suffix = destination.suffix
    if not suffix and infer_suffix:
        suffix = output_suffix(url, fallback=fallback_suffix)
        destination = destination.with_suffix(suffix)
    if unique:
        destination = unique_output_path(destination)
    data = client.get_bytes(url, timeout=timeout, max_bytes=max_bytes)
    if validate is not None:
        validate(data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    from astrid.core.foundation.atomic_io import write_bytes_atomic

    write_bytes_atomic(destination, data)
    return destination, data


def write_journal_atomic(path: str | Path, payload: object) -> None:
    """Write a JSON journal/sidecar with the shared durable atomic writer."""
    write_json_atomic(path, payload)
