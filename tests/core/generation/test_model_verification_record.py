"""Persistent model verification reuses bytes only for the same file and declaration."""
from __future__ import annotations

import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest

from astrid.core.generation import _engine_target_files as target
from astrid.core.generation.engine_contracts import (
    IntegrityError,
    RecipeValidationError,
    UnsafePathError,
)
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from tests.core.generation import test_managed_preparation

fetch_volume = test_managed_preparation.fetch_volume


@pytest.fixture(autouse=True)
def model_verification_default(generation_settings_store):
    # Record regressions exercise the default reuse policy with the key absent.
    generation_settings_store.documents['generation'] = ({}, 1)


def preparation(root):
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.root = str(root)
    prep.deadline = time.monotonic() + 30
    prep.check = lambda: None
    return prep


@pytest.fixture
def model(tmp_path, monkeypatch):
    return make_model(tmp_path, monkeypatch)


def make_model(tmp_path, monkeypatch, name='weight.bin'):
    root = tmp_path / 'engine'
    (root / '.astrid-engine').mkdir(parents=True)
    models = tmp_path / 'models'
    models.mkdir()
    path = models / name
    path.write_bytes(b'weights')
    row = {'name': path.name, 'subdir': '', 'size': path.stat().st_size,
           'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {'models_root': str(models), 'models': [row]}
    reads = []
    original = os.read
    original_stat, original_path_stat = os.fstat, os.stat

    def read(fd, size):
        info, current = original_stat(fd), original_path_stat(path)
        if (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino):
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
    from astrid.core.generation import engine_runner

    # Restore the production method hidden by the storage-only fixture's sentinel.
    del c.prep._verify_alignment
    original_runner = engine_runner._Runner

    class StartupReached(Exception):
        pass

    def stop_startup(**kwargs):
        raise StartupReached

    def runner_for_alignment(plan, *args, **kwargs):
        runner = original_runner(plan, *args, **kwargs)
        runner.plan = plan  # unrelated source/environment checks have no steps
        runner.start = stop_startup
        return runner

    monkeypatch.setattr(engine_runner, '_Runner', runner_for_alignment)
    original = os.read
    reads = []

    def read(fd, size):
        info = os.fstat(fd)
        for path in (c.have, need):
            if path.exists() and (info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino):
                reads.append(path.name)
        return original(fd, size)

    monkeypatch.setattr(target.os, 'read', read)
    with pytest.raises(StartupReached):
        c.fetch()
    assert set(reads) == {c.have.name, need.name}
    reads.clear()
    with pytest.raises(StartupReached):
        c.fetch()
    assert reads == []
    c.have.write_bytes(b'evil')  # same size: preflight alone cannot reject this
    with pytest.raises(IntegrityError, match='managed model bytes differ from the declared digest: have.bin'):
        c.fetch()
    assert c.have.name in reads


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



def test_changed_device_rehashes_different_bytes_at_same_path(model, monkeypatch):
    # Adapted from review2/correctness_probes.py's different-device probe.
    warm(model)
    old = model.path.stat()
    model.path.write_bytes(b'changed')
    current = model.path.stat()
    real_stat, real_fstat = os.stat, os.fstat

    def observed(info):
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            return info
        values = {name: getattr(info, name) for name in
                  ('st_mode', 'st_nlink', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        values.update(st_dev=old.st_dev + 1, st_size=old.st_size, st_ino=old.st_ino,
                      st_mtime_ns=old.st_mtime_ns, st_ctime_ns=old.st_ctime_ns)
        return SimpleNamespace(**values)

    monkeypatch.setattr(target.os, 'stat', lambda *a, **kw: observed(real_stat(*a, **kw)))
    monkeypatch.setattr(target.os, 'fstat', lambda fd: observed(real_fstat(fd)))
    # Under the identical observations, the uncached path sees changed bytes.
    fact = target._TargetFiles(model.manifest['models_root']).file_fact(
        str(model.path), hash_bytes=True, deadline=model.prep.deadline)
    assert fact.sha256 != model.row['sha256']
    model.reads.clear()
    with pytest.raises(IntegrityError, match='managed model bytes differ from the declared digest: weight.bin'):
        model.verify()
    assert model.reads


def test_v1_record_is_a_miss(model):
    warm(model)
    saved = record(model)
    info = model.path.stat()
    value = json.loads(saved.read_text())
    value.update(schema='astrid.model-verification.v1',
                 stat=[info.st_size, info.st_mtime_ns, info.st_ino, info.st_ctime_ns])
    saved.write_text(json.dumps(value))
    model.verify()
    assert model.reads
    assert json.loads(saved.read_text())['schema'] == 'astrid.model-verification.v2'


def test_non_utf8_filename_hashes_then_reuses(tmp_path, monkeypatch):
    if os.name != 'posix':
        pytest.skip('requires POSIX surrogateescape filenames')
    name = os.fsdecode(b'weight-\xff.bin')
    try:
        m = make_model(tmp_path, monkeypatch, name)
    except (OSError, UnicodeError):
        pytest.skip('filesystem does not support this filename')
    assert os.fsencode(m.path.name) == b'weight-\xff.bin'
    warm(m)
    assert json.loads(record(m).read_text())['path'] == str(m.path.resolve())


@pytest.mark.parametrize('error', [OSError('cache unavailable'), RuntimeError('unresolvable path'),
    UnicodeEncodeError('utf-8', '\udcff', 0, 1, 'surrogate'), ValueError('invalid cache name'),
    TypeError('invalid cache input'), RecipeValidationError('invalid cache root')])
def test_constructor_failure_is_a_cache_miss(model, monkeypatch, error):
    from astrid.core.generation import model_verification

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(model_verification, '_VerificationRecord', fail)
    model.verify()
    assert model.reads
    model.reads.clear()
    model.verify()
    assert model.reads


@pytest.mark.parametrize('choice', ['missing-document', 'absent', 'empty-models', True, False,
                                  'invalid', 'invalid-models', 'unreadable', 'unavailable'])
def test_model_verification_policy_with_matching_record(model, generation_settings_store, monkeypatch, choice):
    if choice == 'missing-document':
        generation_settings_store.documents.pop('generation', None)
    elif choice == 'absent':
        generation_settings_store.documents['generation'] = ({}, 1)
    elif choice == 'empty-models':
        generation_settings_store.documents['generation'] = ({'models': {}}, 1)
    elif type(choice) is bool:
        generation_settings_store.documents['generation'] = ({'models': {'trust_unchanged': choice}}, 1)
    elif choice == 'invalid':
        generation_settings_store.documents['generation'] = ({'models': {'trust_unchanged': 'true'}}, 1)
    elif choice == 'invalid-models':
        generation_settings_store.documents['generation'] = ({'models': None}, 1)
    elif choice == 'unavailable':
        from astrid.core.generation import settings
        monkeypatch.setattr(settings, '_store_factory', lambda: None)
    elif choice == 'unreadable':
        def unreadable(key):
            raise OSError('settings store unreadable')
        monkeypatch.setattr(generation_settings_store, 'get_user_app_state', unreadable)
    model.verify()
    assert model.reads and record(model).exists()
    model.reads.clear()
    prep = preparation(model.root)
    prep._verify_models(model.manifest)
    if choice is True or choice in ('missing-document', 'absent', 'empty-models'):
        assert model.reads == []
        assert prep._model_verification.report() == {'trust_unchanged': True, 'hashed': 0, 'skipped': 1}
    else:
        assert model.reads
        assert prep._model_verification.report() == {'trust_unchanged': False, 'hashed': 1, 'skipped': 0}


def test_default_without_record_hashes_fully_and_writes_record(model, generation_settings_store):
    generation_settings_store.documents.pop('generation', None)
    assert not list((model.root / '.astrid-engine/model-verification').glob('*.json'))
    model.verify()
    assert model.reads
    assert json.loads(record(model).read_text())['sha256'] == model.row['sha256']
    assert model.prep._model_verification.report() == {'trust_unchanged': True, 'hashed': 1, 'skipped': 0}


def test_preparation_reads_policy_once_and_logs_hash_skip_counts(model, generation_settings_store, monkeypatch):
    original = generation_settings_store.get_user_app_state
    reads = []

    def read(key):
        reads.append(key)
        return original(key)

    monkeypatch.setattr(generation_settings_store, 'get_user_app_state', read)
    model.prep.logs_root = model.root / 'logs'
    model.verify()
    # A change applies at the next preparation, not between checks in this one.
    generation_settings_store.documents['generation'] = ({'models': {'trust_unchanged': False}}, 2)
    model.reads.clear()
    model.verify()
    assert model.reads == []
    assert reads == ['generation']
    log = model.prep.logs_root / '.preparation-logs/model_verification.stdout.log'
    assert json.loads(log.read_text()) == {'trust_unchanged': True, 'hashed': 1, 'skipped': 1}
    preparation(model.root)._verify_models(model.manifest)
    assert model.reads and reads == ['generation', 'generation']
