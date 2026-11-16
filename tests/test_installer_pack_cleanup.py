"""remove_pack_tree clears read-only bits only on Windows and never through symlinks."""

import errno
import os
import stat

import pytest

from astrid.installer import _pack_cleanup


def _deny_readonly_unlink(monkeypatch, seen):
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        info = os.stat(path, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not info.st_mode & stat.S_IWRITE:
            seen.append(stat.S_IMODE(info.st_mode))
            raise PermissionError(errno.EACCES, "denied", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)


@pytest.mark.posix_native
def test_posix_failed_removal_never_chmods_a_symlink_target_outside_the_tree(tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    outside.chmod(0o640)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(outside)
    _deny_readonly_unlink(monkeypatch, [])

    with pytest.raises(PermissionError):
        _pack_cleanup.remove_pack_tree(tree)

    assert stat.S_IMODE(outside.stat().st_mode) == 0o640


def test_windows_retry_ors_the_write_bit_into_the_existing_mode(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    victim = tree / "object"
    victim.write_text("x")
    victim.chmod(0o454)
    seen = []
    _deny_readonly_unlink(monkeypatch, seen)
    monkeypatch.setattr(_pack_cleanup, "_is_windows", lambda: True)
    modes = []
    real_chmod = os.chmod

    def chmod(path, mode, **kwargs):
        modes.append(mode)
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod)

    _pack_cleanup.remove_pack_tree(tree)

    assert seen == [0o454]
    assert [stat.S_IMODE(m) for m in modes] == [0o454 | stat.S_IWRITE]
    assert not tree.exists()


def test_remove_tree_after_release_rides_out_a_brief_windows_lock(tmp_path, monkeypatch):
    tree = tmp_path / "staging"
    tree.mkdir()
    (tree / "file.node").write_text("x")
    calls, pauses = [], []
    real_remove = _pack_cleanup.remove_pack_tree

    def flaky_remove(path, **kwargs):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError(errno.EACCES, "sharing violation", str(path))
        return real_remove(path, **kwargs)

    monkeypatch.setattr(_pack_cleanup, "_is_windows", lambda: True)
    monkeypatch.setattr(_pack_cleanup, "remove_pack_tree", flaky_remove)

    _pack_cleanup.remove_tree_after_release(tree, sleep=pauses.append)

    assert len(calls) == 3
    assert pauses == [0.25, 0.5]
    assert not tree.exists()


def test_remove_tree_after_release_gives_up_after_bounded_attempts(tmp_path, monkeypatch):
    calls = []

    def always_locked(path, **kwargs):
        calls.append(path)
        raise PermissionError(errno.EACCES, "sharing violation", str(path))

    monkeypatch.setattr(_pack_cleanup, "_is_windows", lambda: True)
    monkeypatch.setattr(_pack_cleanup, "remove_pack_tree", always_locked)

    with pytest.raises(PermissionError):
        _pack_cleanup.remove_tree_after_release(tmp_path, attempts=4, sleep=lambda _s: None)
    assert len(calls) == 4


def test_remove_tree_after_release_does_not_retry_on_posix(tmp_path, monkeypatch):
    calls = []

    def denied(path, **kwargs):
        calls.append(path)
        raise PermissionError(errno.EACCES, "denied", str(path))

    monkeypatch.setattr(_pack_cleanup, "_is_windows", lambda: False)
    monkeypatch.setattr(_pack_cleanup, "remove_pack_tree", denied)

    with pytest.raises(PermissionError):
        _pack_cleanup.remove_tree_after_release(tmp_path, sleep=lambda _s: pytest.fail("slept"))
    assert len(calls) == 1
