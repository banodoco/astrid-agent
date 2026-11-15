"""Storage diagnostics walk metadata without subprocesses or following links."""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import model_storage


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    volume = tmp_path / 'volume'
    root = volume / 'models'
    root.mkdir(parents=True)
    original = Path.read_text
    mounts = f'1 0 0:1 / {volume} rw - ext4 fake rw\n'
    monkeypatch.setattr(Path, 'read_text', lambda path, *a, **kw:
        mounts if str(path) == '/proc/self/mountinfo' else original(path, *a, **kw))
    monkeypatch.setattr(model_storage.subprocess, 'Popen', lambda *a, **kw: pytest.fail('inventory spawned a child'))

    def collect(*, cancelled=lambda: False, deadline=None):
        model_storage.declare_storage_inventory(tmp_path, root,
            deadline=time.monotonic() + 10 if deadline is None else deadline, cancelled=cancelled)
        return (tmp_path / '.preparation-logs/storage.inventory.log').read_text()

    return SimpleNamespace(root=root, volume=volume, collect=collect)


@pytest.mark.parametrize('interrupt', ['deadline', 'cancel'])
def test_inventory_walk_stops_with_partial_listing(inventory, monkeypatch, interrupt):
    child = inventory.volume / 'not-entered'
    child.mkdir()
    scanned, stop = [], [False]
    original = os.scandir
    clock = [1.0]
    monkeypatch.setattr(model_storage.time, 'monotonic', lambda: clock[0])

    def scandir(path):
        scanned.append(path)
        stop[0] = True
        if interrupt == 'deadline':
            clock[0] = 20.0
        return original(path)

    monkeypatch.setattr(model_storage.os, 'scandir', scandir)
    text = inventory.collect(deadline=10, cancelled=lambda: interrupt == 'cancel' and stop[0])
    assert scanned == [inventory.volume]
    assert 'incomplete' in text
    assert str(child) not in text


def test_inventory_walk_never_follows_symlinks(inventory, tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.bin').touch()
    (inventory.root / 'linked').symlink_to(outside, target_is_directory=True)
    (inventory.root / 'loop').symlink_to(inventory.root, target_is_directory=True)
    original, scanned = os.scandir, []

    def scandir(path):
        scanned.append(path)
        return original(path)

    monkeypatch.setattr(model_storage.os, 'scandir', scandir)
    text = inventory.collect()
    assert scanned == [inventory.volume, inventory.root]
    assert 'secret.bin' not in text and 'linked' not in text and 'loop' not in text
    assert 'statvfs' in text


def test_inventory_walk_does_not_enter_another_device(inventory, monkeypatch):
    other = inventory.root / 'other-device'
    other.mkdir()
    original, scanned = os.scandir, []

    class Entry:
        name, path = other.name, str(other)

        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            values = list(other.stat())
            values[2] += 1
            return os.stat_result(values)

    class Scan:
        def __enter__(self):
            return iter([Entry()])

        def __exit__(self, *args):
            pass

    def scandir(path):
        scanned.append(path)
        return Scan() if path == inventory.root else original(path)

    monkeypatch.setattr(model_storage.os, 'scandir', scandir)
    inventory.collect()
    assert scanned == [inventory.volume, inventory.root]


def test_inventory_reports_old_engine_beside_current_models(inventory):
    old = inventory.volume / 'old-engine/version-1'
    old.mkdir(parents=True)
    model = old / 'unreferenced-large-model.bin'
    with model.open('wb') as stream:
        stream.truncate(2**30 + 1)
    text = inventory.collect()
    assert f'{2**30 + 1}\t{model}\n' in text
    assert f'Storage inventory: {inventory.volume}\n' in text


def test_inventory_walk_caps_sorted_rows_and_log_bytes(inventory, monkeypatch):
    original = Path.read_text
    mounts = (f'1 0 0:1 / {inventory.root.parent} rw - ext4 {"x" * 150_000} rw\n'
              '2 0 0:2 / /unrelated-inventory-mount rw - ext4 fake rw\n')
    monkeypatch.setattr(Path, 'read_text', lambda path, *a, **kw:
        mounts if str(path) == '/proc/self/mountinfo' else original(path, *a, **kw))
    for index in range(80):
        directory = inventory.root / f'dir-{index:03}'
        directory.mkdir()
        # Sparse files provide real metadata without consuming model-size disk.
        with (directory / f'model-{index:03}.bin').open('wb') as stream:
            stream.truncate(2**30 + index + 1)
    text = inventory.collect()
    directory_rows, file_rows = text.split('Largest directories (allocated bytes, top 40):\n')[1].split(
        'Files over 1 GiB (logical bytes, top 60):\n')
    assert len(directory_rows.strip().splitlines()) == 40
    files = file_rows.strip().splitlines()
    assert len(files) == 60
    assert files[0].endswith('model-079.bin') and files[-1].endswith('model-020.bin')
    assert '/unrelated-inventory-mount' not in text and 'x' * 8193 not in text
    assert len(text.encode()) <= 128 * 1024
