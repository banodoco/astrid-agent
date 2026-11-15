"""Windows adapter contract and native path parsing, exercised on Linux."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import time
from types import SimpleNamespace

import pytest
from runtime_protocol.dirfd import _WindowsFilesystem

from astrid.core.generation import _engine_target_files as files
from astrid.core.generation.engine_contracts import UnsafePathError
from tests.packs.test_windows_filesystem_seams import WindowsFilesystemSeam


class WindowsPaths(WindowsFilesystemSeam):
    name = 'nt'

    def __getattr__(self, name):
        if name in {'O_CLOEXEC', 'O_NONBLOCK'}:
            raise AttributeError(name)
        return super().__getattr__(name)

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        if path == 'C:\\':
            path = '/'
        # Emulate the adapter's handle-time reparse refusal for the contract
        # test; actual NtCreateFile/GetFileInformationByHandle needs Windows.
        try:
            info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            if _WindowsFilesystem._reject_reparse(getattr(info, 'st_file_attributes', 0),
                                                 nofollow=True, directory=bool(flags & self.O_DIRECTORY)):
                raise OSError('reparse point is not allowed')
        except FileNotFoundError:
            pass
        return super().open(path, flags, mode, dir_fd=dir_fd)


@pytest.fixture
def windows(monkeypatch):
    seam = WindowsPaths()
    monkeypatch.setattr(files, 'os', seam)
    monkeypatch.setattr(files, '_DIR_FLAGS', os.O_RDONLY | seam.O_DIRECTORY | seam.O_NOFOLLOW)
    monkeypatch.setattr(files, '_FILE_FLAGS', os.O_RDONLY | seam.O_NOFOLLOW)
    return seam


def native(path):
    return 'C:' + str(path).replace('/', '\\')


def test_windows_read_write_hash_and_missing(tmp_path, windows):
    target = files._TargetFiles(native(tmp_path))
    deadline = time.monotonic() + 30
    folder = tmp_path / 'models'
    target.ensure_dir(native(folder), deadline=deadline)
    path = folder / 'witness.json'
    target.write_json_atomic(native(path), {'verified': True}, deadline=deadline)
    assert target.read_json(native(path), deadline=deadline) == {'verified': True}
    fact = target.file_fact(native(path), hash_bytes=True, deadline=deadline)
    assert fact.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not target.file_fact(native(folder / 'absent'), hash_bytes=False, deadline=deadline).exists
    assert windows.directory_flushes >= 1
    assert any(parent is not None and flags & windows.O_NOFOLLOW for _, flags, parent in windows.opens)
    assert not list(folder.glob('.a2-*'))


@pytest.mark.parametrize('path', ['relative', 'C:relative', 'C:\\', 'D:\\models\\file',
                                   'C:\\models-other\\file', 'C:\\models\\..\\file',
                                   'C:\\models\\file:stream', 'C:\\models\\.\\file'])
def test_windows_path_refusal(path, windows):
    with pytest.raises(UnsafePathError):
        files._TargetFiles('C:\\models')._relative(path)


def test_windows_drive_case_and_unc(windows):
    assert files._TargetFiles('C:\\Models')._relative('c:/models/child') == ('models', 'child')
    assert files._TargetFiles('\\\\server\\share\\models')._relative(
        '\\\\server\\share\\models\\child') == ('models', 'child')


@pytest.mark.parametrize('reparse', [False, True])
def test_windows_refuses_linked_parent(tmp_path, windows, monkeypatch, reparse):
    real = tmp_path / 'real'
    real.mkdir()
    (real / 'data').write_text('private')
    alias = tmp_path / 'alias'
    if reparse:
        alias.mkdir()
        original = os.stat
        def stat_reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == 'alias':
                return SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return info
        monkeypatch.setattr(os, 'stat', stat_reparse)
    else:
        alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        files._TargetFiles(native(tmp_path)).read_json(native(alias / 'data'), deadline=time.monotonic() + 30)


def test_windows_refuses_linked_leaf(tmp_path, windows):
    (tmp_path / 'source').write_text('{}')
    (tmp_path / 'link').symlink_to(tmp_path / 'source')
    target = files._TargetFiles(native(tmp_path))
    fact = target.file_fact(native(tmp_path / 'link'), hash_bytes=True, deadline=time.monotonic() + 30)
    assert fact.symlink
    with pytest.raises(UnsafePathError):
        target.write_json_atomic(native(tmp_path / 'link'), {}, deadline=time.monotonic() + 30)


def test_verification_record_reused_through_windows_adapter(tmp_path, windows, monkeypatch):
    from astrid.core.generation.model_verification import ModelVerification, model_file_fact
    # pathlib belongs to the actual Linux host; exercise the Windows filesystem
    # contract here, and drive/UNC parsing independently above.
    monkeypatch.setattr(files._TargetFiles, '_split', staticmethod(lambda path: ('/', tuple(files._parts(path)))))
    path = tmp_path / 'model'
    path.write_bytes(b'model bytes')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for hashed, skipped in [(1, 0), (0, 1)]:
        verification = ModelVerification()
        fact = model_file_fact(str(path), root=str(tmp_path), models_root=str(tmp_path),
                               declared_sha256=digest, deadline=time.monotonic() + 30,
                               check=lambda: None, verification=verification)
        assert fact.sha256 == digest
        assert (verification.hashed, verification.skipped) == (hashed, skipped)
    assert len(list((tmp_path / '.astrid/model-verification').glob('*.json'))) == 1


def test_windows_download_directory_flush_and_linux_only_lock(monkeypatch, tmp_path):
    from astrid.core.generation import model_downloader, engine_platforms
    from astrid.core.generation.engine_contracts import UnsupportedPlatform
    from astrid.packs.runpod import engine_snapshot
    windows = SimpleNamespace(name='nt')
    monkeypatch.setattr(model_downloader, 'os', windows)
    model_downloader._fsync_directory(tmp_path)  # must not try CRT directory open
    monkeypatch.setattr(engine_platforms, 'os', windows)
    with pytest.raises(UnsupportedPlatform, match='Linux engine'):
        with engine_platforms.LinuxPlatformAdapter('linux-x86_64-cu130').local_install_lock(
                '/engine', deadline=time.monotonic() + 30):
            pytest.fail('Windows acquired a Linux lock')
    monkeypatch.setattr(engine_snapshot, 'os', windows)
    with pytest.raises(RuntimeError, match='Linux engine'):
        engine_snapshot._Clock(30)
