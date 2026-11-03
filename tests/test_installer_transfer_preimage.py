import hashlib
import os
from pathlib import Path

import pytest

from astrid.installer.transfer_preimage import PreimageUnavailable, read_native_preimage


def test_owned_definition_preimage_preserves_bytes_and_metadata(tmp_path):
    definition = tmp_path / 'astrid.service'
    content = b'[Service]\nExecStart=/old\n'
    definition.write_bytes(content)
    definition.chmod(0o600)
    result = read_native_preimage(definition)
    info = definition.stat()
    assert result.contents == content
    assert "ExecStart" not in repr(result)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert (result.device, result.inode, result.mode, result.uid, result.gid) == (
        info.st_dev, info.st_ino, 0o600, info.st_uid, info.st_gid)


def test_refuses_links_and_keeps_replacement_untouched(tmp_path):
    original = tmp_path / 'original'
    original.write_text('replacement')
    link = tmp_path / 'definition'
    link.symlink_to(original)
    with pytest.raises(PreimageUnavailable):
        read_native_preimage(link)
    parent = tmp_path / 'linked-parent'
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PreimageUnavailable):
        read_native_preimage(parent / 'original')
    assert original.read_text() == 'replacement'


def test_refuses_missing_hardlinked_and_oversized_files(tmp_path):
    definition = tmp_path / 'definition'
    with pytest.raises(PreimageUnavailable):
        read_native_preimage(definition)
    definition.write_bytes(b'1234')
    with pytest.raises(PreimageUnavailable):
        read_native_preimage(definition, max_bytes=3)
    os.link(definition, tmp_path / 'other')
    with pytest.raises(PreimageUnavailable):
        read_native_preimage(definition)
    assert definition.read_bytes() == b'1234'


def test_refuses_replacement_during_read(tmp_path, monkeypatch):
    definition = tmp_path / 'definition'
    replacement = tmp_path / 'replacement'
    definition.write_text('old')
    replacement.write_text('new')
    real_read = os.read
    changed = False

    def read_and_replace(fd, size):
        nonlocal changed
        result = real_read(fd, size)
        if result and not changed:
            changed = True
            os.replace(replacement, definition)
        return result

    monkeypatch.setattr(os, 'read', read_and_replace)
    with pytest.raises(PreimageUnavailable):
        read_native_preimage(definition)
    assert definition.read_text() == 'new'


def test_refuses_nonabsolute_and_parent_traversal(tmp_path):
    with pytest.raises(PreimageUnavailable):
        read_native_preimage(Path('relative'))
    with pytest.raises(PreimageUnavailable):
        read_native_preimage(tmp_path / '..' / 'definition')


def test_refuses_parent_replaced_during_read(tmp_path, monkeypatch):
    parent = tmp_path / 'parent'
    parent.mkdir()
    (parent / 'definition').write_text('old')
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    (replacement / 'definition').write_text('new')
    real_read = os.read
    changed = False

    def read_and_replace(fd, size):
        nonlocal changed
        result = real_read(fd, size)
        if result and not changed:
            changed = True
            parent.rename(tmp_path / 'original-parent')
            replacement.rename(parent)
        return result

    monkeypatch.setattr(os, 'read', read_and_replace)
    with pytest.raises(PreimageUnavailable):
        read_native_preimage(parent / 'definition')
    assert (parent / 'definition').read_text() == 'new'
