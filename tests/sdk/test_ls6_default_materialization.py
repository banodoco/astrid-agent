"""LS-6: count default-view hashing without removing receipt verification."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from astrid.sdk.media_open import _materialize
from tests._local_speed_hashes import OutputHashObserver


@pytest.mark.parametrize("size", [31, 2 * 1024 * 1024 + 17])
@pytest.mark.parametrize("existing", ["absent", "verified", "conflicting"])
def test_default_materialization_hashes_received_buffer_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, existing: str
) -> None:
    from astrid.sdk import storage_root

    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: tmp_path)
    data = (bytes(range(251)) * (size // 251 + 1))[:size]
    digest = hashlib.sha256(data).hexdigest()
    target = tmp_path / "astrid-open" / f"{digest[:12]}-output.bin"
    if existing != "absent":
        target.parent.mkdir()
        target.write_bytes(data if existing == "verified" else b"prior caller bytes")
    meter = OutputHashObserver({digest: len(data)}).install(monkeypatch)

    path, strategy = _materialize(
        data, filename="output.bin", media_type="application/octet-stream", cache_root=None
    )

    # Checking an existing mutable file remains an independent required pass.
    expected_passes = 2 if existing == "verified" else 1
    observation = meter.snapshot()
    assert observation.completed_streams == expected_passes
    assert observation.bytes_hashed == expected_passes * len(data)
    assert observation.coverage_gaps == ()
    assert Path(path).read_bytes() == data
    assert strategy == ("verified-reuse" if existing == "verified" else "verified-copy")
    if existing == "conflicting":
        assert Path(path) != target
        assert target.read_bytes() == b"prior caller bytes"
    else:
        assert Path(path) == target


def test_materialization_does_not_write_through_a_preexisting_temporary_alias(
    tmp_path: Path,
) -> None:
    stored = tmp_path / "stored-object"
    stored.write_bytes(b"immutable stored bytes")
    cache = tmp_path / "views"
    cache.mkdir()
    alias = cache / ".output.bin.tmp"
    os.link(stored, alias)

    path, _ = _materialize(
        b"new output", filename="output.bin", media_type="application/octet-stream",
        cache_root=cache,
    )

    assert stored.read_bytes() == b"immutable stored bytes"
    assert Path(path).read_bytes() == b"new output"
    assert not os.path.samefile(path, stored)


def test_materialization_does_not_reuse_a_hardlink_without_a_location_resolver(
    tmp_path: Path,
) -> None:
    data = b"verified response bytes"
    stored = tmp_path / "stored-object"
    stored.write_bytes(data)
    cache = tmp_path / "views"
    cache.mkdir()
    os.link(stored, cache / "output.bin")

    path, _ = _materialize(
        data, filename="output.bin", media_type="application/octet-stream", cache_root=cache,
    )

    assert not os.path.samefile(path, stored)
    Path(path).write_bytes(b"caller edit")
    assert stored.read_bytes() == data


def test_materialization_sync_failure_preserves_the_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "output.bin"
    target.write_bytes(b"prior caller bytes")

    def fail_sync(_fd: int) -> None:
        raise OSError("injected output sync failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="injected output sync failure"):
        _materialize(
            b"new verified bytes", filename="output.bin", media_type="application/octet-stream",
            cache_root=tmp_path,
        )

    assert target.read_bytes() == b"prior caller bytes"
    assert list(tmp_path.iterdir()) == [target]
