"""Persistent model verification reuses bytes only for the same file and declaration."""
from __future__ import annotations

import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest

from astrid.core.generation import _engine_target_files as target
from astrid.core.generation.engine_contracts import IntegrityError, UnsafePathError
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from tests.core.generation import test_managed_preparation

fetch_volume = test_managed_preparation.fetch_volume


def preparation(root):
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.root = str(root)
    prep.deadline = time.monotonic() + 30
    prep.check = lambda: None
    return prep


@pytest.fixture
def model(tmp_path, monkeypatch):
    root = tmp_path / 'engine'
    (root / '.astrid-engine').mkdir(parents=True)
    models = tmp_path / 'models'
    models.mkdir()
    path = models / 'weight.bin'
    path.write_bytes(b'weights')
    row = {'name': path.name, 'subdir': '', 'size': path.stat().st_size,
           'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {'models_root': str(models), 'models': [row]}
    reads = []
    original = os.read
    original_stat = os.fstat

    def read(fd, size):
        if original_stat(fd).st_ino == path.stat().st_ino:
            reads.append(fd)
        return original(fd, size)

    monkeypatch.setattr(target.os, 'read', read)
    prep = preparation(root)
    return SimpleNamespace(prep=prep, root=root, path=path, row=row, manifest=manifest, reads=reads,
                           verify=lambda: prep._verify_models(manifest))


def warm(model):
    model.verify()
    assert model.reads
    model.reads.clear()
    # A fresh preparation object must reuse the on-disk record.
    preparation(model.root)._verify_models(model.manifest)
    assert model.reads == []


def record(model):
    records = list((model.root / '.astrid-engine/model-verification').glob('*.json'))
    assert len(records) == 1
    return records[0]


def test_second_preparation_reads_no_model_bytes(fetch_volume, monkeypatch):
    c = fetch_volume
    need = c.models / 'checkpoints/need.bin'
    c.prep._native = lambda *a, **kw: need.write_bytes(b'need-more')
    aligned = c.prep._verify_alignment

    def verify(*args):
        c.prep._verify_models({'models_root': str(c.models), 'models': c.rows})
        aligned(*args)

    c.prep._verify_alignment = verify
    original = os.read
    reads = []

    def read(fd, size):
        if os.fstat(fd).st_ino in {p.stat().st_ino for p in (c.have, need) if p.exists()}:
            reads.append(fd)
        return original(fd, size)

    monkeypatch.setattr(target.os, 'read', read)
    c.fetch()
    assert reads
    reads.clear()
    c.fetch()
    assert reads == []


@pytest.mark.parametrize('field', ['st_size', 'st_mtime_ns', 'st_ino', 'st_ctime_ns'])
def test_each_stat_key_field_invalidates_record(model, monkeypatch, field):
    warm(model)
    original = os.fstat
    before = model.path.stat()
    if field == 'st_ino':
        replacement = model.path.with_suffix('.new')
        replacement.write_bytes(b'weights')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(model.path)
        assert model.path.stat().st_ino != before.st_ino
    inode = model.path.stat().st_ino

    def changed(fd):
        info = original(fd)
        if info.st_ino != inode:
            return info
        values = {name: getattr(info, name) for name in
                  ('st_mode', 'st_nlink', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        if field == 'st_ino':
            values['st_mtime_ns'] = before.st_mtime_ns
            values['st_ctime_ns'] = before.st_ctime_ns
        else:
            values[field] += 1
        return SimpleNamespace(**values)

    # Isolate each stat component: utime also changes ctime on a real filesystem.
    monkeypatch.setattr(target.os, 'fstat', changed)
    if field == 'st_size':
        with pytest.raises(UnsafePathError, match='target changed during inspection'):
            model.verify()
    else:
        model.verify()
    assert model.reads


def test_replaced_inode_rehashes_even_with_same_size_and_mtime(model):
    warm(model)
    before = model.path.stat()
    replacement = model.path.with_suffix('.new')
    replacement.write_bytes(b'weights')
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(model.path)
    assert model.path.stat().st_ino != before.st_ino
    model.verify()
    assert model.reads


def test_declared_digest_change_rehashes_and_refuses(model):
    warm(model)
    model.row['sha256'] = '0' * 64
    with pytest.raises(IntegrityError) as caught:
        model.verify()
    assert str(caught.value) == 'managed model bytes differ from the declared digest: weight.bin'
    assert model.reads


@pytest.mark.parametrize('damage', ['json', 'schema', 'path', 'root', 'models_root', 'declared_sha256',
                                    'sha256', 'stat', 'unreadable'])
def test_corrupt_or_foreign_record_rehashes(model, monkeypatch, damage):
    warm(model)
    saved = record(model)
    if damage == 'json':
        saved.write_text('{')
    elif damage == 'unreadable':
        original = target._TargetFiles.read_json

        def unreadable(self, path, **kwargs):
            if path == str(saved):
                raise PermissionError('unreadable record')
            return original(self, path, **kwargs)

        monkeypatch.setattr(target._TargetFiles, 'read_json', unreadable)
    else:
        value = json.loads(saved.read_text())
        value[damage] = 'foreign'
        saved.write_text(json.dumps(value))
    model.verify()
    assert model.reads
    assert json.loads(saved.read_text())['sha256'] == model.row['sha256']


def test_different_bytes_rehash_refuses_and_does_not_record_mismatch(model):
    warm(model)
    saved = record(model).read_bytes()
    model.path.write_bytes(b'changed')
    with pytest.raises(IntegrityError) as caught:
        model.verify()
    assert str(caught.value) == 'managed model bytes differ from the declared digest: weight.bin'
    assert model.reads
    assert record(model).read_bytes() == saved


def test_storage_preflight_and_alignment_share_record(model, monkeypatch):
    from astrid.core.generation import model_storage

    missing = {**model.row, 'name': 'missing.bin'}
    manifest = {**model.manifest, 'models': [model.row, missing]}
    monkeypatch.setattr(model_storage, 'available_model_bytes', lambda *a, **kw: (10**9, False))
    model.prep._preflight_model_storage(manifest)
    assert model.reads
    model.reads.clear()
    model.verify()
    assert model.reads == []
    preparation(model.root)._preflight_model_storage(manifest)
    assert model.reads == []


def test_first_mismatch_never_writes_verification_record(model):
    model.row['sha256'] = '0' * 64
    with pytest.raises(IntegrityError, match='managed model bytes differ from the declared digest: weight.bin'):
        model.verify()
    assert model.reads
    assert not list((model.root / '.astrid-engine/model-verification').glob('*.json'))


def test_unwritable_record_still_checks_model_bytes(model, monkeypatch):
    original = target._TargetFiles.write_json_atomic

    def unwritable(self, path, *args, **kwargs):
        if '/model-verification/' in path:
            raise PermissionError('cannot persist optional record')
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(target._TargetFiles, 'write_json_atomic', unwritable)
    model.verify()
    assert model.reads
    model.reads.clear()
    model.verify()
    assert model.reads


def test_file_changed_during_record_lookup_is_refused(model, monkeypatch):
    warm(model)
    saved = record(model)
    original = target._TargetFiles.read_json

    def racing(self, path, **kwargs):
        value = original(self, path, **kwargs)
        if path == str(saved):
            model.path.write_bytes(b'changed')
        return value

    monkeypatch.setattr(target._TargetFiles, 'read_json', racing)
    with pytest.raises(UnsafePathError, match='target changed during inspection'):
        model.verify()


def test_symlink_model_keeps_existing_integrity_refusal(model):
    model.path.unlink()
    model.path.symlink_to(model.path.name)
    with pytest.raises(IntegrityError) as caught:
        model.verify()
    assert str(caught.value) == 'managed model bytes differ from the declared digest: weight.bin'
    assert model.reads == []
