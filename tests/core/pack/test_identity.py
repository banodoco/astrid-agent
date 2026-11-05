from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.core.pack.identity import pack_digest, read_pin, write_pin


def _published_tree(root: Path) -> None:
    (root / "shared").mkdir(parents=True)
    (root / "pack.yaml").write_text(
        'schema_version: 3\nid: hello_ext\nname: Hello\nversion: 1.0.0\npack_api: "1.0"\n',
        encoding="utf-8",
        newline="",
    )
    (root / "shared" / "util.py").write_text("VALUE = 1\n", encoding="utf-8", newline="")


def test_pack_digest_matches_published_pack_tree_v1_vector(tmp_path: Path) -> None:
    _published_tree(tmp_path)

    assert hashlib.sha256((tmp_path / "pack.yaml").read_bytes()).hexdigest() == (
        "7babd167ce7b7a2f7bd895f4f9c3df7598bd55b392c4b44ec6d4bccac97656fa"
    )
    assert hashlib.sha256((tmp_path / "shared/util.py").read_bytes()).hexdigest() == (
        "e13df8c44af5dea1e412403910b99cc5a48f2ccbf68a66b3374d6ab9cef9fc65"
    )
    assert pack_digest(tmp_path) == "1af20ca4fc53a2792fad43c8298d9b2982925c1e3bb6620bda3e93e557dbe2db"


def test_digest_omits_pin_builds_caches_and_bytecode_but_keeps_nested_pin(tmp_path: Path) -> None:
    _published_tree(tmp_path)
    initial = pack_digest(tmp_path)
    (tmp_path / "PIN").write_text("ignored\n", encoding="utf-8")
    (tmp_path / ".build").mkdir()
    (tmp_path / ".build" / "artifact").write_text("ignored", encoding="utf-8")
    (tmp_path / "shared" / "__pycache__").mkdir()
    (tmp_path / "shared" / "__pycache__" / "util.pyc").write_bytes(b"ignored")
    (tmp_path / "shared" / "PIN").write_text("payload\n", encoding="utf-8")

    assert pack_digest(tmp_path) != initial
    (tmp_path / "shared" / "PIN").unlink()
    assert pack_digest(tmp_path) == initial


def test_digest_rejects_symlink_even_when_named_as_excluded_directory(tmp_path: Path) -> None:
    _published_tree(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    try:
        (tmp_path / ".build").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="symlinks"):
        pack_digest(tmp_path)


def test_pin_round_trip_has_exact_c4_fields_and_terminal_newline(tmp_path: Path) -> None:
    pin = {
        "version": 1,
        "source": "https://example.invalid/poms_packs.git#hello_ext",
        "commit": "1" * 40,
        "digest_rule": "pack-tree-v1",
        "digest": "a" * 64,
        "origin": "user",
    }
    path = tmp_path / "PIN"

    write_pin(path, pin)

    assert path.read_bytes().endswith(b"\n")
    assert json.loads(path.read_text(encoding="utf-8")) == pin
    assert read_pin(path) == pin


@pytest.mark.parametrize("origin", ["folder", "personal"])
@pytest.mark.parametrize("source", ["file:///packs/personal", "file:/packs/personal", "file://localhost/packs/personal"])
def test_pin_accepts_absolute_local_file_sources_for_unpinned_origins(
    tmp_path: Path, source: str, origin: str
) -> None:
    path = tmp_path / "PIN"
    value = {
        "version": 1,
        "source": source,
        "commit": None,
        "digest_rule": "pack-tree-v1",
        "digest": None,
        "origin": origin,
    }
    write_pin(path, value)
    assert read_pin(path) == value

    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="newline"):
        read_pin(path)


@pytest.mark.parametrize(
    "source",
    ["file://remote-host/path", "file:relative"],
)
@pytest.mark.parametrize("origin", ["folder", "personal"])
def test_pin_rejects_nonlocal_or_nonabsolute_file_sources(
    tmp_path: Path, source: str, origin: str
) -> None:
    path = tmp_path / "PIN"
    value = {
        "version": 1,
        "source": source,
        "commit": None,
        "digest_rule": "pack-tree-v1",
        "digest": None,
        "origin": origin,
    }
    with pytest.raises(ValueError, match="local file URIs"):
        write_pin(path, value)


def test_pin_rejects_non_file_source_for_unpinned_origins(tmp_path: Path) -> None:
    path = tmp_path / "PIN"
    value = {
        "version": 1,
        "source": "https://example.invalid/personal",
        "commit": None,
        "digest_rule": "pack-tree-v1",
        "digest": None,
        "origin": "personal",
    }
    with pytest.raises(ValueError, match="local file URIs"):
        write_pin(path, value)


def test_pin_requires_newline_and_local_unpinned_origins(tmp_path: Path) -> None:
    path = tmp_path / "PIN"
    value = {
        "version": 1,
        "source": "file:///packs/personal",
        "commit": None,
        "digest_rule": "pack-tree-v1",
        "digest": None,
        "origin": "personal",
    }
    write_pin(path, value)
    assert read_pin(path) == value

    invalid = {**value, "source": "https://example.invalid/personal"}
    with pytest.raises(ValueError, match="local file URIs"):
        write_pin(path, invalid)


def test_digest_changes_when_payload_bytes_change(tmp_path: Path) -> None:
    _published_tree(tmp_path)
    before = pack_digest(tmp_path)
    (tmp_path / "shared" / "util.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert pack_digest(tmp_path) != before
