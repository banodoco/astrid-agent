"""Declared volume admission and post-fetch storage diagnostics, without a pod."""
from __future__ import annotations

import errno
import json
import os
import time
from concurrent.futures import CancelledError
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core._shared import run_logs
from astrid.core.generation import local_job, model_storage
from astrid.core.generation.engine_contracts import CommandResult, IntegrityError
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from tests.core.generation import test_managed_preparation

fetch_volume = test_managed_preparation.fetch_volume


def volume_job(c, tmp_path, monkeypatch, *, volume=True):
    """Read the same manifest envelope the claimed machine ships to the pod."""
    body = {"schema": local_job.SCHEMA, "align": "align", "inputs": {}}
    if volume:
        body["storage_volume"] = {"id": "fake-claim-volume", "size_gb": 10,
                                  "mount_path": str(c.models.parents[1])}
    path = tmp_path / 'job.json'
    path.write_text(json.dumps(body))
    monkeypatch.setattr(local_job, '_run_job', lambda *a, **kw: c.fetch())
    return lambda: local_job.run_job('workflow', path, tmp_path / 'out')


def allocated(root):
    paths = [root, *root.rglob('*')]
    seen, used = set(), 0
    for path in paths:
        info = path.lstat()
        identity = (info.st_dev, info.st_ino)
        if identity not in seen:
            seen.add(identity)
            used += info.st_blocks * 512
    return used


@pytest.mark.parametrize('refused', [True, False])
def test_claimed_volume_counts_whole_mount_before_native_fetch(fetch_volume, tmp_path, monkeypatch,
                                                             refused):
    c = fetch_volume
    mount = c.models.parents[1]
    old = mount / 'old-engine.bin'
    old.write_bytes(b'x' * 8192)
    # A sparse existing engine consumes the rest of this fake 10 GiB volume.
    # Fake only its allocated metadata; all paths and the walk are real.
    needed = 8192
    c.rows[1]['size'] = needed
    remaining = 4096 if refused else needed
    adjustment = 10 * 2**30 - allocated(mount) - remaining
    original_scandir = os.scandir
    scanned = []

    class Entry:
        def __init__(self, entry):
            self._entry = entry
            self.name, self.path = entry.name, entry.path

        def stat(self, *, follow_symlinks):
            info = self._entry.stat(follow_symlinks=follow_symlinks)
            if self.name == old.name:
                return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino,
                    st_mode=info.st_mode, st_blocks=info.st_blocks + adjustment // 512)
            return info

    class Scan:
        def __init__(self, path):
            scanned.append(os.fstat(path).st_ino if isinstance(path, int) else Path(path).stat().st_ino)
            self.scan = original_scandir(path)

        def __enter__(self):
            return (Entry(entry) for entry in self.scan)

        def __exit__(self, *args):
            self.scan.close()

    with monkeypatch.context() as patch:
        patch.setattr(model_storage.os, 'scandir', Scan)
        c.state.query_failure = True
        run = volume_job(c, tmp_path, monkeypatch)
        if refused:
            with pytest.raises(IntegrityError) as caught:
                run()
            assert caught.value.code == 'insufficient_storage'
            assert caught.value.details == {'needed_bytes': needed, 'free_bytes': remaining}
            assert c.native == []
        else:
            run()
            assert c.native == ['fetch']
        assert mount.stat().st_ino in scanned


def measure(root, *, deadline=None, check=lambda: None):
    volume = model_storage.DeclaredVolume(root, 10)
    return model_storage.measure_declared_volume(volume, root / 'models',
        deadline=time.monotonic() + 20 if deadline is None else deadline, check=check)


def test_declared_gb_uses_larger_binary_reading_and_allocated_bytes(tmp_path):
    (tmp_path / 'models').mkdir()
    sparse = tmp_path / 'old-engine.bin'
    with sparse.open('wb') as stream:
        stream.truncate(2**30)
    measured = measure(tmp_path)
    assert measured.free_bytes == 10 * 2**30 - allocated(tmp_path)
    assert measured.free_bytes > 10 * 10**9 - allocated(tmp_path)


def test_volume_counts_hardlinks_once_and_does_not_follow_symlinks(tmp_path):
    root = tmp_path / 'mount'
    root.mkdir()
    (root / 'models').mkdir()
    (root / 'one').write_bytes(b'x' * 8192)
    (root / 'two').hardlink_to(root / 'one')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'large').write_bytes(b'x' * 16384)
    (root / 'link').symlink_to(outside, target_is_directory=True)
    (root / 'loop').symlink_to(root, target_is_directory=True)
    (root / 'long-link').symlink_to('/outside/' + 'x' * 1000)
    assert measure(root).free_bytes == 10 * 2**30 - allocated(root)


@pytest.mark.parametrize('deadline,stopped_at', [(120, 106), (1000, 161)])
def test_volume_walk_deadline_is_unknown(tmp_path, monkeypatch, deadline, stopped_at):
    (tmp_path / 'models').mkdir()
    clock = [100.0]
    original = os.scandir

    def scan(path):
        clock[0] = stopped_at  # quarter of remaining time, capped at 60 seconds
        return original(path)

    monkeypatch.setattr(model_storage.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(model_storage.os, 'scandir', scan)
    assert measure(tmp_path, deadline=deadline) is None


def test_volume_walk_checks_cancellation(tmp_path, monkeypatch):
    (tmp_path / 'models').mkdir()
    scanning = []
    original = os.scandir

    def scan(path):
        scanning.append(True)
        return original(path)

    def check():
        if scanning:
            raise CancelledError('cancelled while walking')

    monkeypatch.setattr(model_storage.os, 'scandir', scan)
    with pytest.raises(CancelledError, match='cancelled while walking'):
        measure(tmp_path, check=check)
    assert scanning == [True]


@pytest.mark.parametrize('volume', [False, True])
def test_unknown_or_absent_volume_proceeds_with_existing_measurements(fetch_volume, tmp_path,
                                                                     monkeypatch, volume):
    c = fetch_volume
    c.state.query_failure = True
    run = volume_job(c, tmp_path, monkeypatch, volume=volume)
    if volume:
        monkeypatch.setattr(model_storage, 'measure_declared_volume', lambda *a, **kw: None)
    run()
    assert c.native == ['fetch']


@pytest.fixture
def native_fetch(tmp_path, monkeypatch):
    from astrid.core.generation.engine_platforms import platform_adapter
    from astrid.core.generation.engine_recipe import load_recipe

    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.check = lambda: None
    prep.root, prep.task, prep.attempt = str(tmp_path), 'quota-task', 'attempt'
    prep.mode = 'align'
    prep.recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    prep.adapter = platform_adapter(prep.recipe.platform)
    prep.deadline = time.monotonic() + 20
    prep.cancelled = lambda: False
    prep.logs_root = tmp_path / 'out'
    models = tmp_path / 'models'
    models.mkdir()
    prep._storage_models_root = models
    original = Path.read_text
    mounts = f'1 0 0:1 / {tmp_path} rw - ext4 fake rw\n'
    monkeypatch.setattr(Path, 'read_text', lambda path, *a, **kw:
        mounts if str(path) == '/proc/self/mountinfo' else original(path, *a, **kw))
    return prep


@pytest.mark.parametrize('number', [errno.EDQUOT, errno.ENOSPC])
@pytest.mark.parametrize('reported', [False, True], ids=['exception', 'diagnostic'])
def test_fetch_space_failure_declares_inventory_and_preserves_original(native_fetch, number, reported):
    prep = native_fetch
    failure = OSError(number, os.strerror(number))
    if reported:
        result = CommandResult(1, f'Failed: {failure}', '', False, 0.1, None, False, False)
        prep.context = SimpleNamespace(run=lambda request: result)
        expected = ('managed alignment command failed (step=fetch, reason=model_fetch_failed); '
                    'its whole output is the run logs preparation.fetch.stdout and preparation.fetch.stderr')
        error_type = IntegrityError
    else:
        def fail(request):
            raise failure
        prep.context = SimpleNamespace(run=fail)
        expected, error_type = str(failure), OSError
    with pytest.raises(error_type) as caught:
        prep._command(['fetch', 'workflow.py'])
    assert str(caught.value) == expected
    if not reported:
        assert caught.value is failure and caught.value.errno == number
    else:
        assert caught.value.code == 'integrity_failed'
    declared = {decl.name: path for decl, path in run_logs.read_declared(prep.logs_root)}
    assert 'storage.inventory' in declared
    assert 'Storage inventory:' in declared['storage.inventory'].read_text()
    assert any('astrid tasks logs quota-task' in note and 'storage.inventory' in note
               for note in caught.value.__notes__)


@pytest.mark.parametrize('number,step', [(errno.EACCES, 'fetch'), (errno.EDQUOT, 'nodes')])
def test_other_failures_do_not_inventory(native_fetch, monkeypatch, number, step):
    prep = native_fetch
    calls = []
    monkeypatch.setattr(model_storage, 'declare_storage_inventory', lambda *a, **kw: calls.append(1))
    failure = OSError(number, os.strerror(number))
    def fail(request):
        raise failure
    prep.context = SimpleNamespace(run=fail)
    with pytest.raises(OSError) as caught:
        prep._command([step, 'workflow.py'])
    assert caught.value is failure
    assert calls == []


def test_inventory_error_does_not_mask_fetch_failure(native_fetch, monkeypatch):
    calls = []
    failure = OSError(errno.EDQUOT, 'original quota failure')
    def inventory(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('broken inventory')
    def fail(request):
        raise failure
    monkeypatch.setattr(model_storage, 'declare_storage_inventory', inventory)
    native_fetch.context = SimpleNamespace(run=fail)
    with pytest.raises(OSError) as caught:
        native_fetch._command(['fetch', 'workflow.py'])
    assert caught.value is failure and str(caught.value) == '[Errno 122] original quota failure'
    assert calls == [True]


def test_declared_volume_only_applies_to_its_mount_device(tmp_path):
    root = tmp_path / 'mount'
    root.mkdir()
    (root / 'models').mkdir()
    volume = model_storage.DeclaredVolume(root, 10)
    assert model_storage.measure_declared_volume(volume, tmp_path / 'elsewhere',
        deadline=time.monotonic() + 20, check=lambda: None) is None


def test_job_volume_context_is_restored_after_failure(fetch_volume, tmp_path, monkeypatch):
    c = fetch_volume
    c.state.query_failure = True
    run = volume_job(c, tmp_path, monkeypatch)
    scopes = []
    def fail(*args, **kwargs):
        scopes.append(model_storage.DECLARED_VOLUME.get())
        raise RuntimeError('job stopped')
    monkeypatch.setattr(local_job, '_run_job', fail)
    with pytest.raises(RuntimeError, match='job stopped'):
        run()
    assert scopes[0].size_gb == 10
    assert model_storage.DECLARED_VOLUME.get() is None


@pytest.mark.parametrize('diagnostic', ['EDQUOT', 'ENOSPC', 'Disk quota exceeded',
                                       'No space left on device'])
def test_native_fetch_recognizes_reported_storage_diagnostics(native_fetch, diagnostic):
    prep = native_fetch
    prep.context = SimpleNamespace(run=lambda request:
        CommandResult(1, '', diagnostic, False, 0.1, None, False, False))
    with pytest.raises(IntegrityError):
        prep._command(['fetch', 'workflow.py'])
    assert 'storage.inventory' in {decl.name for decl, _ in run_logs.read_declared(prep.logs_root)}


def test_volume_walk_skips_another_device(tmp_path, monkeypatch):
    root = tmp_path / 'mount'
    root.mkdir()
    (root / 'models').mkdir()
    other = root / 'other-device'
    other.mkdir()
    (other / 'large').write_bytes(b'x' * 8192)
    original = os.scandir
    scanned = []

    class Entry:
        def __init__(self, entry):
            self.entry = entry
            self.name = entry.name

        def stat(self, *, follow_symlinks):
            info = self.entry.stat(follow_symlinks=follow_symlinks)
            return SimpleNamespace(st_dev=info.st_dev + (self.name == 'other-device'),
                st_ino=info.st_ino, st_mode=info.st_mode, st_blocks=info.st_blocks)

    class Scan:
        def __init__(self, path):
            scanned.append(os.fstat(path).st_ino if isinstance(path, int) else Path(path).stat().st_ino)
            self.scan = original(path)

        def __enter__(self):
            return (Entry(entry) for entry in self.scan)

        def __exit__(self, *args):
            self.scan.close()

    with monkeypatch.context() as patch:
        patch.setattr(model_storage.os, 'scandir', Scan)
        observation = measure(root)
    assert observation.free_bytes == 10 * 2**30 - root.stat().st_blocks * 512 - (root / 'models').stat().st_blocks * 512
    assert other.stat().st_ino not in scanned


def test_nonstorage_native_diagnostic_does_not_inventory(native_fetch):
    prep = native_fetch
    prep.context = SimpleNamespace(run=lambda request:
        CommandResult(1, '', 'PermissionError: [Errno 13] Permission denied', False, 0.1, None, False, False))
    with pytest.raises(IntegrityError):
        prep._command(['fetch', 'workflow.py'])
    assert 'storage.inventory' not in {decl.name for decl, _ in run_logs.read_declared(prep.logs_root)}


def test_fetch_storage_inventory_survives_failure_receipt(native_fetch, tmp_path):
    from astrid.packs.runpod.failure_receipt import build_receipt, write_receipt

    prep = native_fetch
    prep.context = SimpleNamespace(run=lambda request:
        CommandResult(1, '', 'OSError: [Errno 122] Disk quota exceeded', False, 0.1, None, False, False))
    with pytest.raises(IntegrityError) as caught:
        prep._command(['fetch', 'workflow.py'])
    rows = run_logs.collect(prep.logs_root)
    transported = [{**row, 'path': str(prep.logs_root / 'logs' / row['file'])} for row in rows]
    lease = SimpleNamespace(failure_logs={'files': transported})
    receipt = build_receipt({'failure': caught.value, 'stage': 'fetch', 'lease': lease},
        job=SimpleNamespace(task_id=prep.task), machine=None, timing=None)
    written = json.loads(write_receipt(receipt, root=tmp_path / 'failures').read_text())
    row = next(row for row in written['logs'] if row['name'] == 'storage.inventory')
    assert not row['missing']
    assert 'Storage inventory:' in Path(row['path']).read_text()


@pytest.mark.parametrize('source', ['statvfs', 'quota'])
def test_declared_capacity_does_not_override_a_tighter_existing_measurement(fetch_volume, tmp_path,
                                                                          monkeypatch, source):
    c = fetch_volume
    if source == 'statvfs':
        c.state.fs_free = 8
    else:
        c.state.free = 8
    run = volume_job(c, tmp_path, monkeypatch)
    with pytest.raises(IntegrityError) as caught:
        run()
    assert caught.value.details == {'needed_bytes': 9, 'free_bytes': 8}
    assert c.native == []


def test_declared_volume_available_when_statvfs_and_quota_are_unknown(fetch_volume, tmp_path, monkeypatch):
    c = fetch_volume
    c.state.fs_unavailable = c.state.query_failure = True
    run = volume_job(c, tmp_path, monkeypatch)
    # Prove this observation supplies a real budget even without either older source.
    c.rows[1]['size'] = 10 * 2**30
    with pytest.raises(IntegrityError) as caught:
        run()
    assert caught.value.code == 'insufficient_storage'
    assert caught.value.details['free_bytes'] == 10 * 2**30 - allocated(c.models.parents[1])
    assert c.native == []
