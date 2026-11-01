"""Fake-only checks for read-only A03 preparation; no installer is run."""
from dataclasses import replace
import json

import pytest

from astrid.core.generation.engine_install_task import InstallBinding, prepare_install, recheck_install


def test_plan_is_read_only_and_retains_unknown_disk_requirement(tmp_path):
    root = tmp_path / 'new' / 'engine'
    binding, disk = prepare_install(root=str(root))
    assert not root.exists()
    assert binding.root == str(root)
    assert binding.engine == 'vibecomfy-h3'
    assert binding.python.startswith(str(root) + '/')
    assert binding.python_version
    assert len(binding.recipe_sha256) == len(binding.models_lock_sha256) == 64
    assert disk.path == str(tmp_path)
    assert disk.available_bytes >= 0
    assert disk.locked_model_bytes > 0
    assert disk.required_additional_bytes is None
    assert disk.sufficiency == 'unknown'
    assert recheck_install(binding).path == disk.path


def test_binding_change_requires_new_admission(tmp_path):
    root = tmp_path / 'new' / 'engine'
    binding, _ = prepare_install(root=str(root))
    with pytest.raises(ValueError, match='binding changed'):
        recheck_install(replace(binding, recipe_sha256='0' * 64))
    with pytest.raises(ValueError, match='binding changed'):
        recheck_install(replace(binding, python='/tmp/other'))
    (tmp_path / 'new').mkdir()
    with pytest.raises(ValueError, match='binding changed'):
        recheck_install(binding)


def test_symlink_path_and_other_engine_are_rejected(tmp_path):
    (tmp_path / 'link').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        prepare_install(root=str(tmp_path / 'link' / 'engine'))
    with pytest.raises(ValueError, match='only the indexed'):
        prepare_install(root=str(tmp_path / 'engine'), engine='wan2gp')


def test_small_free_space_is_observed_without_inventing_required_space(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from astrid.core.generation import engine_install_task

    monkeypatch.setattr(engine_install_task.os, 'fstatvfs',
                        lambda fd: SimpleNamespace(f_bavail=1, f_frsize=4096))
    _, disk = prepare_install(root=str(tmp_path / 'engine'))
    assert disk.available_bytes == 4096
    assert disk.locked_model_bytes > disk.available_bytes
    assert disk.required_additional_bytes is None
    assert disk.sufficiency == 'unknown'


def test_disk_observation_failure_is_not_silently_ignored(tmp_path, monkeypatch):
    from astrid.core.generation import engine_install_task

    def fail(_fd):
        raise OSError('disk unavailable')

    monkeypatch.setattr(engine_install_task.os, 'fstatvfs', fail)
    with pytest.raises(OSError, match='disk unavailable'):
        prepare_install(root=str(tmp_path / 'engine'))


def test_binding_round_trips_as_task_data_and_is_rechecked(tmp_path):
    binding, _ = prepare_install(root=str(tmp_path / 'engine'))
    restored = InstallBinding.from_dict(json.loads(json.dumps(binding.as_dict())))
    assert restored == binding
    assert recheck_install(restored).path == str(tmp_path)
    altered = restored.as_dict()
    altered['models_lock_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='binding changed'):
        recheck_install(InstallBinding.from_dict(altered))


@pytest.mark.parametrize('change', [
    lambda d: d.pop('root'),
    lambda d: d.update(extra='ignored'),
    lambda d: d.update(root=''),
    lambda d: d.update(anchor_inode=True),
    lambda d: d.update(anchor_device=-1),
])
def test_binding_rejects_malformed_task_data(tmp_path, change):
    binding, _ = prepare_install(root=str(tmp_path / 'engine'))
    document = binding.as_dict()
    change(document)
    with pytest.raises(ValueError, match='invalid managed install binding'):
        InstallBinding.from_dict(document)
