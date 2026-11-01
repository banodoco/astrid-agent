"""Fake-only checks for read-only A03 preparation; no installer is run."""
from dataclasses import replace
from unittest.mock import Mock

import pytest

from astrid.core.generation.engine_install_task import (
    prepare_install, recheck_install, tracked_install_route,
)


def test_tracked_install_route_refuses_without_installing(tmp_path, monkeypatch):
    from astrid.core.generation import engine_runner

    install_engine = Mock()
    runner = Mock()
    monkeypatch.setattr(engine_runner, 'install_engine', install_engine)
    monkeypatch.setattr(engine_runner, '_Runner', runner)

    result = tracked_install_route(root=str(tmp_path / 'engine'))

    assert result['code'] == 'MANAGED_INSTALL_ROUTE_DISABLED'
    install_engine.assert_not_called()
    runner.assert_not_called()
    assert not list(tmp_path.rglob('install.json'))


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
