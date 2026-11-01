from __future__ import annotations

from pathlib import Path

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.foundation import atomic_io
from astrid.core.util.generation_helpers import (
    download_output,
    load_provider_key,
    output_suffix,
    write_journal_atomic,
)
from astrid.core.util.http import HttpClient


def test_output_suffix_is_stable_for_query_urls_and_media_fallbacks() -> None:
    assert output_suffix("https://cdn.test/result.MP4?token=redacted") == ".mp4"
    assert output_suffix("https://cdn.test/result", fallback="mp3") == ".mp3"
    assert output_suffix("https://cdn.test/result?file=.webm", fallback=".png") == ".png"
    with pytest.raises(ValueError, match="unsupported output suffix"):
        output_suffix("https://cdn.test/result", fallback=".exe")


def test_download_output_enforces_bound_and_uses_deterministic_suffix(tmp_path: Path) -> None:
    client = HttpClient(transport=lambda _request: (200, b"audio"))
    destination, data = download_output(
        client,
        "https://cdn.test/object?download=1",
        tmp_path / "generated",
        timeout=2,
        max_bytes=5,
        fallback_suffix=".wav",
    )
    assert destination.name == "generated.wav"
    assert destination.read_bytes() == data == b"audio"

    oversized = HttpClient(transport=lambda _request: (200, b"too large"))
    with pytest.raises(AstridError, match="bounded body limit"):
        download_output(
            oversized,
            "https://cdn.test/object.wav",
            tmp_path / "oversized.wav",
            timeout=2,
            max_bytes=3,
        )
    assert not (tmp_path / "oversized.wav").exists()

    original = tmp_path / "requested-without-suffix"
    exact_path, _ = download_output(
        client,
        "https://cdn.test/asset.wav",
        original,
        timeout=2,
        max_bytes=5,
        infer_suffix=False,
    )
    assert exact_path == original


def test_provider_key_uses_credential_precedence_and_scrubs_registered_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "astrid.env"
    shared.write_text("FAL_KEY='shared-key-value'\n", encoding="utf-8")
    fallback = tmp_path / "fallback.env"
    fallback.write_text("FAL_KEY='fallback-key-value'\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(shared))
    monkeypatch.setenv("FAL_KEY", "environment-key-value")

    assert load_provider_key("fal", env_file=fallback) == "shared-key-value"
    assert load_provider_key("fal", env_file=fallback, explicit="explicit-key") == "explicit-key"
    empty_shared = tmp_path / "empty-astrid.env"
    empty_shared.write_text("", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(empty_shared))
    assert load_provider_key("fal", env_file=fallback) == "environment-key-value"
    monkeypatch.delenv("FAL_KEY")
    assert load_provider_key("fal", env_file=fallback) == "fallback-key-value"

    client = HttpClient(transport=lambda _request: (200, b"{}"))
    client.register_secret("shared-key-value")
    assert client.scrub_secret("request failed with shared-key-value") == "request failed with ***"


def test_journal_write_is_atomic_and_keeps_previous_file_on_promotion_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.json"
    write_journal_atomic(path, {"state": "old"})
    old_bytes = path.read_bytes()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated promotion failure")

    monkeypatch.setattr(atomic_io.platform, "durable_replace", fail_replace)
    with pytest.raises(atomic_io.AtomicWriteError, match="simulated promotion failure"):
        write_journal_atomic(path, {"state": "new"})

    assert path.read_bytes() == old_bytes
    assert list(tmp_path.glob(".journal.json.*.tmp")) == []
