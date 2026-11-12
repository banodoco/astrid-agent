"""Windows filesystem constraints, with real staging and POSIX exchange for rollback."""

from __future__ import annotations

import errno
import os
import stat
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.pack import install as installer, transaction
from astrid.core.pack.identity import pack_digest, read_pin
from astrid.pack_api import VERSION


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pack.yaml").write_text(
        'schema_version: 3\nid: hello_ext\nname: Hello\nversion: 1.0.0\n'
        f'pack_api: "{VERSION[0]}.{VERSION[1]}"\n'
        'documentation: {kind: none, reason: Windows publication fixture.}\n'
    )
    (source / "nested").mkdir()
    (source / "nested/payload.txt").write_text("old\n")
    return source


@pytest.fixture
def windows_fs(monkeypatch):
    # Patch only the installer's OS view: pathlib and shutil still need the
    # actual host's path classes and cleanup implementation on this runner.
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = "nt"
    opened_directories, synced_files, replacements = [], [], []
    modes = {}
    path_open = Path.open

    def open_path(path, mode="r", *args, **kwargs):
        stream = path_open(path, mode, *args, **kwargs)
        modes[stream.fileno()] = mode
        return stream

    def open_descriptor(fd, mode="r", *args, **kwargs):
        stream = os.fdopen(fd, mode, *args, **kwargs)
        modes[stream.fileno()] = mode
        return stream

    def open_file(path, flags, *args, **kwargs):
        if Path(path).is_dir():
            opened_directories.append(path)
            raise PermissionError(errno.EACCES, "Windows CRT cannot open directories", str(path))
        return os.open(path, flags, *args, **kwargs)

    def fsync(fd):
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        # _commit/FlushFileBuffers requires a handle with write access.
        if not any(flag in modes[fd] for flag in ("+", "w", "a")):
            raise OSError(errno.EBADF, "Windows cannot flush a read-only handle")
        synced_files.append(os.fstat(fd).st_ino)
        os.fsync(fd)

    def replace(stage, destination):
        replacements.append((stage, destination))
        os.replace(stage, destination)

    windows_os.open, windows_os.fsync, windows_os.replace = open_file, fsync, replace
    windows_os.fdopen = open_descriptor
    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(installer, "os", windows_os)
    monkeypatch.setattr(installer.sys, "platform", "win32")
    return SimpleNamespace(os=windows_os, opened_directories=opened_directories,
                           synced_files=synced_files, replacements=replacements)


def install(source, root, **kwargs):
    return installer.install_pack(source=source, pack_id="hello_ext", packs_root=root, **kwargs)


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("missing_directory_flag", [False, True])
def test_windows_install_flushes_files_and_atomically_publishes(
        source, tmp_path, windows_fs, monkeypatch, missing_directory_flag):
    if missing_directory_flag:
        monkeypatch.delattr(windows_fs.os, "O_DIRECTORY", raising=False)
    root = tmp_path / "packs"
    policy = installer.PublicationPolicy.for_platform("win32")
    result = install(source, root, publication=policy)

    assert result.changed
    assert (result.path / "nested/payload.txt").read_text() == "old\n"
    assert result.observed_digest == pack_digest(result.path)
    assert read_pin(result.path / "PIN")["origin"] == "folder"
    assert len(windows_fs.synced_files) == 3  # manifest, payload, PIN
    assert windows_fs.opened_directories == []
    assert len(windows_fs.replacements) == 1
    assert windows_fs.replacements[0][1] == result.path
    assert not list(root.glob(".install-*"))
    assert not install(source, root, publication=policy).changed


def test_windows_replace_failure_does_not_publish(source, tmp_path, windows_fs, monkeypatch):
    root = tmp_path / "packs"

    def denied(stage, destination):
        raise PermissionError(errno.EACCES, "sharing violation", str(destination))

    monkeypatch.setattr(windows_fs.os, "replace", denied)
    with pytest.raises(installer.PackInstallError, match="stage=publish.*PermissionError.*errno=13"):
        install(source, root, publication=installer.PublicationPolicy.for_platform("win32"))
    assert not (root / "hello_ext").exists()
    assert not list(root.glob(".install-*"))


def test_windows_updates_keep_the_complete_prior_install(source, tmp_path, windows_fs):
    root = tmp_path / "packs"
    policy = installer.PublicationPolicy.for_platform("win32")
    first = install(source, root, publication=policy)
    before, inode = snapshot(first.path), first.path.stat().st_ino
    (source / "nested/payload.txt").write_text("new\n")

    # Windows replace cannot overwrite a nonempty directory. Never emulate an
    # exchange with move-old/move-new, which would lose crash atomicity.
    with pytest.raises(installer.PackInstallError) as failure:
        install(source, root, publication=policy)
    assert failure.value.code == "atomic_swap_unsupported"
    assert snapshot(first.path) == before
    assert first.path.stat().st_ino == inode
    assert len(windows_fs.replacements) == 1
    assert not list(root.glob(".install-*"))


@pytest.mark.parametrize(("seam", "stage"), [
    ("_copy_folder", "copy_source"), ("_sync_tree", "sync_tree"), ("_publish", "publish"),
])
@pytest.mark.parametrize("kind", ["oserror", "valueerror"])
def test_failure_diagnostic_is_sanitized_and_preserves_old_install(
        source, tmp_path, windows_fs, monkeypatch, caplog, seam, stage, kind):
    root = tmp_path / "packs"
    first = install(source, root)
    before, inode = snapshot(first.path), first.path.stat().st_ino
    (source / "nested/payload.txt").write_text("new\n")
    secret = "private-path-with-secret-token"
    error = (OSError(errno.EIO, secret, str(tmp_path / secret)) if kind == "oserror"
             else ValueError(secret))

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(installer, seam, fail)
    with pytest.raises(installer.PackInstallError) as failure:
        install(source, root)
    diagnostic = f"stage={stage}, error={type(error).__name__}, errno={error.errno if kind == 'oserror' else None}"
    assert failure.value.code == "install_failed"
    assert diagnostic in str(failure.value)
    assert diagnostic in caplog.text
    visible_error = "".join(traceback.format_exception(failure.value))
    for output in (caplog.text, visible_error):
        assert secret not in output
        assert str(tmp_path) not in output
    assert snapshot(first.path) == before
    assert first.path.stat().st_ino == inode
    assert not list(root.glob(".install-*"))


def test_windows_file_fsync_errors_are_not_skipped(source, tmp_path, windows_fs, monkeypatch):
    root = tmp_path / "packs"
    first = install(source, root)
    before = snapshot(first.path)
    (source / "nested/payload.txt").write_text("new\n")

    def fail(fd):
        raise OSError(errno.EIO, "file flush failed")

    monkeypatch.setattr(windows_fs.os, "fsync", fail)
    with pytest.raises(installer.PackInstallError, match="stage=sync_tree.*errno=5"):
        install(source, root)
    assert snapshot(first.path) == before
    assert len(windows_fs.replacements) == 1


@pytest.mark.skipif(os.name == "nt", reason="Requires real POSIX directory fsync.")
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_posix_sync_flushes_files_and_all_directories(source, monkeypatch, platform):
    synced = set()

    def fsync(fd):
        info = os.fstat(fd)
        synced.add((info.st_dev, info.st_ino))
        os.fsync(fd)

    posix_os = SimpleNamespace(**vars(os))
    posix_os.fsync = fsync
    monkeypatch.setattr(installer, "os", posix_os)
    monkeypatch.setattr(installer.sys, "platform", platform)
    installer._sync_tree(source)
    assert synced == {(p.stat().st_dev, p.stat().st_ino) for p in [source, *source.rglob("*")]}


@pytest.mark.skipif(os.name == "nt", reason="Exercises POSIX directory-open failure.")
def test_posix_directory_open_failure_is_not_ignored(source, tmp_path, monkeypatch):
    root = tmp_path / "packs"
    first = install(source, root)
    before = snapshot(first.path)
    (source / "nested/payload.txt").write_text("new\n")
    posix_os = SimpleNamespace(**vars(os))

    def deny_directory(path, flags, *args, **kwargs):
        if Path(path).is_dir():
            raise PermissionError(errno.EACCES, "directory flush denied", str(path))
        return os.open(path, flags, *args, **kwargs)

    posix_os.open = deny_directory
    monkeypatch.setattr(installer, "os", posix_os)
    with pytest.raises(installer.PackInstallError, match="stage=sync_tree.*errno=13"):
        install(source, root)
    assert snapshot(first.path) == before
    assert not list(root.glob(".install-*"))


@pytest.mark.skipif(os.name == "nt", reason="Tests Windows sync with real POSIX atomic exchange.")
def test_windows_sync_preserves_group_update_and_rollback(source, tmp_path, windows_fs, monkeypatch):
    # Keep the real host's already-bound exchange: this proves the Windows
    # sync change preserves transaction rollback, not Windows native exchange.
    monkeypatch.delattr(windows_fs.os, "O_DIRECTORY", raising=False)
    monkeypatch.setattr(transaction, "os", windows_fs.os)
    root = tmp_path / "packs"
    kwargs = dict(source=source, packs={"hello_ext": "."}, packs_root=root, python_packages={})
    transaction.install_group(**kwargs)
    old_pack = snapshot(root / "hello_ext")
    (source / "nested/payload.txt").write_text("new\n")
    updated = transaction.install_group(**kwargs, intent="update")
    assert (root / "hello_ext/nested/payload.txt").read_text() == "new\n"

    def interrupt(phase):
        if phase == "before_swap":
            raise OSError(errno.EIO, "rollback interrupted")

    current = snapshot(root)
    with pytest.raises(OSError, match="rollback interrupted"):
        transaction.rollback_group(packs_root=root, pack_ids=("hello_ext",),
                                   transaction_id=updated.transaction_id, boundary=interrupt)
    assert snapshot(root) == current
    restored = transaction.rollback_group(packs_root=root, pack_ids=("hello_ext",),
                                          transaction_id=updated.transaction_id)
    assert snapshot(root / "hello_ext") == old_pack
    assert restored.revision == updated.revision + 1
    assert windows_fs.synced_files
    assert windows_fs.opened_directories == []


def test_readonly_git_objects_are_removed_on_windows_permission_failure(tmp_path, monkeypatch):
    import shutil
    from astrid.installer import _pack_cleanup

    # The injected unlink failure models Windows' read-only behavior on every
    # host, so exercise the matching read-only-bit retry branch as well.
    monkeypatch.setattr(_pack_cleanup, "_is_windows", lambda: True)

    original_unlink = os.unlink

    def windows_unlink(path, *args, **kwargs):
        info = os.stat(path, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False)
        if not info.st_mode & stat.S_IWRITE:
            raise PermissionError(errno.EACCES, "read-only", str(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", windows_unlink)
    tree = tmp_path / "objects.git"
    pack = tree / "objects/pack"
    pack.mkdir(parents=True)
    for suffix in ("idx", "pack"):
        file = pack / ("pack-abc." + suffix)
        file.write_bytes(b"git object")
        file.chmod(stat.S_IREAD)
    with pytest.raises(PermissionError):
        shutil.rmtree(tree)
    _pack_cleanup.remove_pack_tree(tree)
    assert not tree.exists()


def test_committed_install_records_cleanup_failure_and_keeps_residue(source, tmp_path, monkeypatch, caplog):
    root = tmp_path / "packs"
    first = install(source, root)
    (source / "nested/payload.txt").write_text("new\n")
    real_remove = installer.remove_pack_tree

    def refuse_cleanup(path, **kwargs):
        if path.name.startswith(".install-"):
            raise PermissionError(errno.EACCES, "busy")
        return real_remove(path, **kwargs)

    monkeypatch.setattr(installer, "remove_pack_tree", refuse_cleanup)
    result = install(source, root)
    assert result.changed and result.path == first.path
    assert (result.path / "nested/payload.txt").read_text() == "new\n"
    residues = list(root.parent.glob(".install-*"))
    assert len(residues) == 1
    assert (residues[0] / "hello_ext/nested/payload.txt").read_text() == "old\n"
    assert str(residues[0]) in caplog.text
    real_remove(residues[0])
