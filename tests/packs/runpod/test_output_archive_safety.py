"""Caller archive boundary: portable paths and validate before publishing."""

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import machine as rm

UNSAFE = ['..', '../x', '/x', 'D:x', 'C:\\x', 'a\\b', 'CON', 'con.txt', 'CON .txt',
          'PRN', 'AUX.txt', 'NUL', 'COM1', 'COM9.txt', 'LPT1', 'LPT9.txt',
          'a/CON.txt', 'a:b', '', '.', './x', 'a//b', 'a/./b', 'a/',
          'trailing.', 'trailing ', 'a./b', '//server/share/x']


def _bundle(tmp_path, entries, *, member_type=None, bad_digest=None):
    files = {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
             for name, data in entries}
    if bad_digest is not None:
        files[bad_digest]['sha256'] = '0' * 64
    body = json.dumps({'files': files}).encode()
    bundle = tmp_path / 'source.tar'
    with tarfile.open(bundle, 'w') as archive:
        for name, data in [(rm.MANIFEST_NAME, body), *entries]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            if member_type and name != rm.MANIFEST_NAME:
                member.type, member.linkname, member.size = member_type, 'elsewhere', 0
            archive.addfile(member, io.BytesIO(data))
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    calls = []

    def get(remote, local):
        calls.append(remote)
        shutil.copyfile(remote, local)

    def fetch(destination):
        return rm._fetch_output_archive(SimpleNamespace(get=get), str(bundle), destination,
                                        scratch, hashlib.sha256(body).hexdigest())

    return fetch, calls


@pytest.mark.parametrize('name', UNSAFE)
def test_archive_rejects_portably_unsafe_members_without_writing(tmp_path, name):
    fetch, _ = _bundle(tmp_path, [('a-good', b'good'), (name, b'bad')])
    destination = tmp_path / 'outputs'
    with pytest.raises(rm.DigestMismatch):
        fetch(destination)
    assert not destination.exists() or not list(destination.iterdir())
    assert not (tmp_path / 'x').exists()


@pytest.mark.parametrize('path_type', [PurePosixPath, PureWindowsPath])
@pytest.mark.parametrize('name', UNSAFE)
def test_path_predicate_rejects_under_both_semantics(name, path_type):
    assert not rm._safe_archive_path(name, path_type)


@pytest.mark.parametrize('path_type', [PurePosixPath, PureWindowsPath])
def test_path_predicate_accepts_nested_regular_names(path_type):
    assert rm._safe_archive_path('artifacts/cpu.png', path_type)
    assert rm._safe_archive_path('COM10/LPT10.txt', path_type)


@pytest.mark.parametrize('member_type', [tarfile.LNKTYPE, tarfile.SYMTYPE])
def test_archive_rejects_links(tmp_path, member_type):
    fetch, _ = _bundle(tmp_path, [('image', b'bad')], member_type=member_type)
    with pytest.raises(rm.DigestMismatch):
        fetch(tmp_path / 'outputs')


@pytest.mark.parametrize('link', ['hardlink', 'symlink', 'parent_symlink'])
def test_archive_refuses_existing_link_before_modifying_any_file(tmp_path, link):
    outside = tmp_path / 'outside'
    outside.mkdir()
    original = outside / 'image'
    original.write_bytes(b'original')
    destination = tmp_path / 'outputs'
    destination.mkdir()
    target = destination / 'z-image'
    if link == 'hardlink':
        target.hardlink_to(original)
    elif link == 'symlink':
        target.symlink_to(original)
    else:
        target.symlink_to(outside, target_is_directory=True)
    name = 'z-image/image' if link == 'parent_symlink' else 'z-image'
    fetch, _ = _bundle(tmp_path, [('a-good', b'good'), (name, b'bad')])
    with pytest.raises(rm.DigestMismatch):
        fetch(destination)
    assert original.read_bytes() == b'original'
    assert not (destination / 'a-good').exists()


def test_bad_late_digest_leaves_all_existing_files_intact(tmp_path):
    destination = tmp_path / 'outputs'
    destination.mkdir()
    existing = destination / 'a-existing'
    existing.write_bytes(b'original')
    fetch, _ = _bundle(tmp_path, [('a-existing', b'replacement'), ('b-new', b'new'),
                                  ('z-bad', b'bad')], bad_digest='z-bad')
    with pytest.raises(rm.DigestMismatch):
        fetch(destination)
    assert existing.read_bytes() == b'original'
    assert sorted(path.name for path in destination.iterdir()) == ['a-existing']


@pytest.mark.parametrize('names', [('a', 'a/b'), ('image', 'IMAGE')])
def test_conflicting_paths_are_rejected_before_publishing(tmp_path, names):
    fetch, _ = _bundle(tmp_path, [(name, b'bad') for name in names])
    destination = tmp_path / 'outputs'
    with pytest.raises(rm.DigestMismatch):
        fetch(destination)
    assert not destination.exists() or not list(destination.iterdir())


def test_verified_archive_uses_one_download_and_renames_staged_files(tmp_path, monkeypatch):
    destination = tmp_path / 'outputs'
    destination.mkdir()
    (destination / 'old').write_bytes(b'old')
    fetch, calls = _bundle(tmp_path, [('old', b'new'), ('nested/image', b'image')])
    replaces = []
    original_replace = rm.os.replace

    def replace(source, target):
        assert Path(source).is_relative_to(destination)
        if Path(source) == destination / 'old':
            assert Path(source).read_bytes() == b'old'
            assert Path(target).name.startswith('original-')
        else:
            assert Path(source).read_bytes() in (b'new', b'image')
            assert Path(source).parent.name.startswith('.astrid-output-')
        replaces.append(target)
        original_replace(source, target)

    monkeypatch.setattr(rm.os, 'replace', replace)
    files = fetch(destination)
    assert len(calls) == 1
    assert len(replaces) == 3
    assert Path(replaces[0]).name.startswith('original-')
    assert Path(files['old']).read_bytes() == b'new'
    assert Path(files['nested/image']).read_bytes() == b'image'
    assert sorted(path.name for path in destination.iterdir()) == ['nested', 'old']


@pytest.mark.parametrize('fail_at', [2, 3, 4, 5])
def test_any_publish_rename_failure_restores_originals(tmp_path, monkeypatch, fail_at):
    destination = tmp_path / 'outputs'
    destination.mkdir()
    originals = {'a-existing': b'first original', 'z-existing': b'last original'}
    for name, data in originals.items():
        (destination / name).write_bytes(data)
    fetch, calls = _bundle(tmp_path, [('a-existing', b'first replacement'),
                                    ('b-new', b'new'), ('z-existing', b'last replacement')])
    replace = rm.os.replace
    renames = []

    def fail_once(source, target):
        renames.append((source, target))
        if len(renames) == fail_at:
            raise OSError('injected rename failure')
        return replace(source, target)

    monkeypatch.setattr(rm.os, 'replace', fail_once)
    with pytest.raises(OSError, match='injected rename failure'):
        fetch(destination)
    assert calls and len(calls) == 1  # Rollback never retries the transfer.
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == originals


def test_resolved_destination_escape_is_refused_before_link_check(tmp_path):
    destination, outside = tmp_path / 'outputs', tmp_path / 'outside'
    destination.mkdir()
    outside.mkdir()
    (destination / 'parent').symlink_to(outside, target_is_directory=True)
    fetch, _ = _bundle(tmp_path, [('parent/image', b'bad')])
    # The reason pins the independent resolve-inside guard: generic link
    # refusal alone cannot satisfy this regression.
    with pytest.raises(rm.DigestMismatch, match='escapes the output directory'):
        fetch(destination)
    assert not list(outside.iterdir())
