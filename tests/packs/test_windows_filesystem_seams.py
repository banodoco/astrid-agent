"""Exercise pack custody with Windows adapter flags on a POSIX filesystem.

This seam checks the callers' adapter contract; native handles, ACLs and reparse
points still require the Runtime's native Windows qualification tests.
"""
from __future__ import annotations

import os
import stat

import pytest

from runtime_protocol.dirfd import _WindowsFilesystem, filesystem

pytestmark = pytest.mark.posix_native


class WindowsFilesystemSeam:
    O_DIRECTORY = _WindowsFilesystem.O_DIRECTORY
    O_NOFOLLOW = _WindowsFilesystem.O_NOFOLLOW
    O_NONBLOCK = _WindowsFilesystem.O_NONBLOCK

    def __init__(self):
        self.opens = []
        self.directory_flushes = 0

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        self.opens.append((path, flags, dir_fd))
        native_flags = flags & ~(self.O_DIRECTORY | self.O_NOFOLLOW)
        if flags & self.O_DIRECTORY:
            native_flags |= os.O_DIRECTORY
        if flags & self.O_NOFOLLOW:
            native_flags |= os.O_NOFOLLOW
        return os.open(path, native_flags, mode, dir_fd=dir_fd)

    def fsync(self, fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            self.directory_flushes += 1  # The Windows adapter cannot flush directories.
        else:
            os.fsync(fd)


class WindowsReviewPublicationSeam:
    name = "nt"

    def __init__(self):
        self.file_flushes = 0

    def __getattr__(self, name):
        if name == "O_DIRECTORY":
            raise AttributeError(name)
        return getattr(os, name)

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        if os.path.isdir(path):
            raise AssertionError("review publication must not open a directory on Windows")
        return os.open(path, flags, mode, dir_fd=dir_fd)

    def fsync(self, fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise AssertionError("review publication must not fsync a directory on Windows")
        self.file_flushes += 1
        os.fsync(fd)


@pytest.fixture
def windows_filesystem(monkeypatch):
    seam = WindowsFilesystemSeam()

    def install(module):
        assert module.os is filesystem, "custody must use the Runtime filesystem adapter"
        monkeypatch.setattr(module, "os", seam)
        return seam

    return install


def test_dataset_review_capture_is_bounded_and_confined(tmp_path, windows_filesystem):
    from astrid.packs.training.actions.dataset_build import review_assets

    windows_filesystem(review_assets)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "media").write_bytes(b"captured")
    assert review_assets._capture(tmp_path, "nested/media", 8) == b"captured"
    with pytest.raises(ValueError, match="bounded"):
        review_assets._capture(tmp_path, "nested/media", 7)
    (tmp_path / "alias").symlink_to(nested, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        review_assets._capture(tmp_path, "alias/media", 8)
    with pytest.raises(ValueError, match="confined"):
        review_assets._capture(tmp_path, "../outside", 8)


def test_dataset_review_capture_detects_replaced_input(tmp_path, monkeypatch, windows_filesystem):
    from astrid.packs.training.actions.dataset_build import review_assets

    seam = windows_filesystem(review_assets)
    path = tmp_path / "media"
    path.write_bytes(b"original")
    original_read = os.read
    replaced = False

    def replace_during_read(fd, size):
        nonlocal replaced
        data = original_read(fd, size)
        if not replaced:
            replaced = True
            path.rename(tmp_path / "old")
            path.write_bytes(b"replaced")
        return data

    monkeypatch.setattr(seam, "read", replace_during_read, raising=False)
    with pytest.raises(ValueError, match="changed or was replaced"):
        review_assets._capture(tmp_path, "media", 8)


def test_human_review_publication_and_retirement_use_adapter(tmp_path, monkeypatch, windows_filesystem):
    from astrid.packs.editorial.actions.human_review import run as review
    from astrid.core.execution import process_group

    seam = windows_filesystem(review)
    monkeypatch.setattr(process_group, "process_birth_identity", lambda: "selected-birth")
    monkeypatch.setenv(review._REVIEW_LAUNCH_NONCE_ENV, "a" * 64)
    protected = []
    owner_only = review.platform.owner_only

    def protect(fd):
        protected.append(fd)
        owner_only(fd)

    monkeypatch.setattr(review.platform, "owner_only", protect)
    path = tmp_path / "receiver.json"
    url = "http://127.0.0.1:17560/?token=" + "a" * 32
    owner = review._publish_receiver_session(path, url)
    assert protected and seam.directory_flushes == 1
    with pytest.raises(FileExistsError):
        review._publish_receiver_session(path, url)
    assert not list(tmp_path.glob(".review-session-*"))
    review._retire_receiver_session(owner)
    assert not path.exists()


def test_human_review_publication_skips_directory_fsync_on_windows(tmp_path, monkeypatch):
    from astrid.packs.editorial.actions.human_review import run as review
    from astrid.core.execution import process_group

    seam = WindowsReviewPublicationSeam()
    monkeypatch.setattr(review, "os", seam)
    monkeypatch.setattr(process_group, "process_birth_identity", lambda: "selected-birth")
    monkeypatch.setenv(review._REVIEW_LAUNCH_NONCE_ENV, "a" * 64)
    path = tmp_path / "receiver.json"
    url = "http://127.0.0.1:17560/?token=" + "a" * 32

    owner = review._publish_receiver_session(path, url)

    assert owner[3] == path.read_bytes()
    assert seam.file_flushes == 1
    assert not list(tmp_path.glob(".review-session-*"))


def test_human_review_retirement_preserves_replacement(tmp_path, monkeypatch, windows_filesystem):
    from astrid.packs.editorial.actions.human_review import run as review
    from astrid.core.execution import process_group

    windows_filesystem(review)
    monkeypatch.setattr(process_group, "process_birth_identity", lambda: "selected-birth")
    monkeypatch.setenv(review._REVIEW_LAUNCH_NONCE_ENV, "a" * 64)
    path = tmp_path / "receiver.json"
    owner = review._publish_receiver_session(path, "http://127.0.0.1:17560/?token=" + "a" * 32)
    path.rename(tmp_path / "original")
    path.write_bytes(owner[3])
    with pytest.raises(review.HumanReviewInputError, match="owner mismatch"):
        review._retire_receiver_session(owner)
    assert path.exists()


def test_frozen_cleanup_removes_owned_tree_without_following_links(tmp_path, windows_filesystem):
    from astrid.packs.rendering.actions.timeline_visualize import frozen

    windows_filesystem(frozen)
    root = tmp_path / "reconstructed"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "asset").write_bytes(b"owned")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "asset").write_bytes(b"keep")
    (root / "link").symlink_to(foreign, target_is_directory=True)
    frozen._register_rehydrated_pack(root)
    frozen._discard_rehydrated_root(root)
    assert not root.exists()
    assert (foreign / "asset").read_bytes() == b"keep"


def test_frozen_cleanup_preserves_replaced_root(tmp_path, windows_filesystem):
    from astrid.packs.rendering.actions.timeline_visualize import frozen

    windows_filesystem(frozen)
    root = tmp_path / "reconstructed"
    root.mkdir()
    frozen._register_rehydrated_pack(root)
    root.rename(tmp_path / "original")
    root.mkdir()
    (root / "foreign").write_bytes(b"keep")
    frozen._discard_rehydrated_root(root)
    assert (root / "foreign").read_bytes() == b"keep"
