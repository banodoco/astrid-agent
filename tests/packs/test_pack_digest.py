from __future__ import annotations

from pathlib import Path

import pytest

from astrid.core.pack import pack_digest, read_pin, write_pin


def _write(root: Path, name: str, content: bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_pack_digest_is_path_order_and_metadata_independent(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write(first, "z/file.bin", b"z")
    _write(first, "a/file.bin", b"a")
    _write(second, "a/file.bin", b"a")
    _write(second, "z/file.bin", b"z")

    assert pack_digest(first) == pack_digest(second)


def test_pack_digest_ignores_pin_build_output_and_cache_trees(tmp_path: Path) -> None:
    _write(tmp_path, "source.py", b"source")
    expected = pack_digest(tmp_path)
    _write(tmp_path, "PIN", b"0" * 64 + b"\n")
    _write(tmp_path, ".build/generated.py", b"generated")
    _write(tmp_path, "nested/__pycache__/module.pyc", b"bytecode")
    _write(tmp_path, ".pytest_cache/state", b"cache")
    _write(tmp_path, ".cache/tool/state", b"cache")

    assert pack_digest(tmp_path) == expected


def test_pack_digest_changes_when_source_path_or_bytes_change(tmp_path: Path) -> None:
    _write(tmp_path, "source.py", b"one")
    original = pack_digest(tmp_path)
    (tmp_path / "source.py").write_bytes(b"two")
    assert pack_digest(tmp_path) != original


def test_pack_digest_rejects_symlinked_pack_content(tmp_path: Path) -> None:
    target = tmp_path / "target"
    root = tmp_path / "pack"
    target.write_text("outside", encoding="utf-8")
    root.mkdir()
    try:
        (root / "linked").symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink"):
        pack_digest(root)


def test_pin_round_trip_does_not_change_pack_digest(tmp_path: Path) -> None:
    _write(tmp_path, "source.py", b"source")
    digest = pack_digest(tmp_path)

    assert read_pin(tmp_path) is None
    write_pin(tmp_path, digest)
    assert read_pin(tmp_path) == digest
    assert pack_digest(tmp_path) == digest


def test_pin_rejects_invalid_values_and_contents(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lowercase 64-character"):
        write_pin(tmp_path, "A" * 64)
    (tmp_path / "PIN").write_text("not-a-digest\n", encoding="ascii")
    with pytest.raises(ValueError, match="invalid pack PIN"):
        read_pin(tmp_path)
