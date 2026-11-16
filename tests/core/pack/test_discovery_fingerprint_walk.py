"""LS-12 exact fingerprint compatibility; no metadata-only admission shortcut."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import stat
import pytest
from astrid.core.pack.discovery import _tree_fingerprint


def reference(root):
    # The exact pre-LS-12 algorithm is the compatibility oracle.
    digest = hashlib.sha256()
    def add(value):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    def visit(path):
        add(os.fsencode(path.relative_to(root)))
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            add(b"missing")
            return
        add(str(mode).encode())
        if stat.S_ISLNK(mode):
            add(os.fsencode(os.readlink(path)))
        elif stat.S_ISDIR(mode):
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(mode):
            with path.open("rb") as stream:
                add(hashlib.file_digest(stream, "sha256").digest())
        else:
            raise OSError(f"cannot fingerprint non-regular pack resource: {path}")
    visit(root)
    return digest.hexdigest()


def corpus(tmp_path):
    root = tmp_path / "pack"
    root.mkdir()
    for relative, data in [("pack.yaml", b"name: before"), ("Z/data.bin", bytes(range(256))),
                           ("a/empty", b""), (".hidden", b"hidden"),
                           ("zeta", b"last"), ("unicode-\u00e9", b"utf8")]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "empty-dir").mkdir()
    return root


@pytest.mark.parametrize("kind", ["tree", "empty", "file", "missing", "relative"])
def test_exact_reference_digest(tmp_path, monkeypatch, kind):
    root = corpus(tmp_path)
    if kind == "empty":
        root = root / "empty-dir"
    elif kind == "file":
        root = root / "pack.yaml"
    elif kind == "missing":
        root = root / "absent"
    elif kind == "relative":
        monkeypatch.chdir(tmp_path)
        root = Path("pack")
    assert _tree_fingerprint(root) == reference(root)


@pytest.mark.parametrize("edit", ["same-size-restored-time", "add-file", "remove-file",
                                   "rename", "add-directory", "change-hidden", "kind-change"])
def test_every_content_and_name_change_is_detected(tmp_path, edit):
    root = corpus(tmp_path)
    before = _tree_fingerprint(root)
    target = root / "pack.yaml"
    if edit == "same-size-restored-time":
        previous = target.stat()
        target.write_bytes(b"name: after!")
        assert target.stat().st_size == previous.st_size
        os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif edit == "add-file":
        (root / "untracked").write_bytes(b"new")
    elif edit == "remove-file":
        target.unlink()
    elif edit == "rename":
        target.rename(root / "pack.yml")
    elif edit == "add-directory":
        (root / "other-empty-dir").mkdir()
    elif edit == "change-hidden":
        (root / ".hidden").write_bytes(b"changed")
    elif edit == "kind-change":
        target.unlink()
        target.mkdir()
    actual = _tree_fingerprint(root)
    assert actual != before
    assert actual == reference(root)


def test_symlink_and_broken_symlink_are_not_followed(tmp_path):
    root = corpus(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    value = external / "secret-resource"
    value.write_bytes(b"first")
    (root / "link").symlink_to(external, target_is_directory=True)
    (root / "broken").symlink_to(tmp_path / "absent")
    before = _tree_fingerprint(root)
    assert before == reference(root)
    value.write_bytes(b"second")
    assert _tree_fingerprint(root) == before
    (root / "broken").unlink()
    (root / "broken").symlink_to(tmp_path / "different-absent")
    assert _tree_fingerprint(root) != before
    assert _tree_fingerprint(root) == reference(root)


def test_root_symlink_preserves_reference_semantics(tmp_path):
    root = corpus(tmp_path)
    link = tmp_path / "root-link"
    link.symlink_to(root, target_is_directory=True)
    assert _tree_fingerprint(link) == reference(link)


def test_all_regular_files_are_rehashed_each_call(tmp_path, monkeypatch):
    root = corpus(tmp_path)
    original = hashlib.file_digest
    seen = []
    def observe(stream, *args, **kwargs):
        seen.append(Path(stream.name).relative_to(root).as_posix())
        return original(stream, *args, **kwargs)
    monkeypatch.setattr(hashlib, "file_digest", observe)
    first = _tree_fingerprint(root)
    count = len(seen)
    second = _tree_fingerprint(root)
    assert first == second
    assert count == 6 and len(seen) == 2 * count
    assert seen[:count] == seen[count:]


def test_contents_unreadable_still_raise(tmp_path, monkeypatch):
    root = corpus(tmp_path)
    original = hashlib.file_digest
    def unavailable(stream, *args, **kwargs):
        if Path(stream.name).name == "pack.yaml":
            raise OSError("controlled unreadable fixture")
        return original(stream, *args, **kwargs)
    monkeypatch.setattr(hashlib, "file_digest", unavailable)
    with pytest.raises(OSError, match="controlled unreadable fixture"):
        _tree_fingerprint(root)
