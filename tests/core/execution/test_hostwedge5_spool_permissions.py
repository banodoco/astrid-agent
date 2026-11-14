"""Default spool privacy starts below an ordinary shared data root."""
from __future__ import annotations

# Fixtures are imported and consumed by argument name.
# ruff: noqa: F811
import json
import os
import stat
from pathlib import Path

import pytest

from astrid.core.execution.generic_host import GenericPackHost, HostError
from tests.core.execution.test_bug14_settlement_liveness import settlement_case  # noqa: F401
from tests.core.execution.test_hostwedge_recovery_scope import private_home  # noqa: F401
from tests.core.execution.test_terminal_publication_replay import _saved_failure
from tests.test_generic_host_result_json import host_case  # noqa: F401


def _root(tmp_path, monkeypatch, mode=0o755):
    root = tmp_path / "data"
    root.mkdir(mode=mode)
    root.chmod(mode)
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(root))
    return root


def _refused(call, path, fault):
    with pytest.raises(HostError) as error:
        call()
    message = str(error.value)
    assert str(path) in message
    assert fault in message
    assert "repair" in message or "remove" in message


def _metadata_owner(monkeypatch, target, uid):
    identity = target.stat()
    original_lstat, original_fstat = Path.lstat, os.fstat

    def replace(metadata):
        if (metadata.st_dev, metadata.st_ino) == (identity.st_dev, identity.st_ino):
            fields = list(metadata)
            fields[4] = uid
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw: replace(original_lstat(path, *a, **kw)))
    monkeypatch.setattr(os, "fstat", lambda fd: replace(original_fstat(fd)))


def test_umask_022_data_root_starts_and_recovers_default_spool(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    data = _root(tmp_path, monkeypatch)
    monkeypatch.setattr(case.host, "claim_once", lambda: None)
    assert case.host.run(once=True) == []
    _, request = _saved_failure(case, tmp_path)
    case.host.attempt_root = None
    root = case.host._allocate_attempt_root(case.task_id, case.attempt["attempt_id"])
    (root / "fail-request.json").write_text(json.dumps(request))
    assert root.parent.parent.parent == data
    for directory in (data / "attempts", root.parent, root):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert directory.stat().st_uid == os.getuid()
    restarted = GenericPackHost(pack_roots=[], client=case.host.client, executor_id=case.host.executor_id)
    monkeypatch.setattr(restarted, "claim_once", lambda: None)
    assert restarted.run(once=True) == []
    assert len(case.failures) == 1
    assert case.service.task(case.task_id)["task"]["status"] == "failed"
    assert json.loads((root / "fail-request.json").read_text()) == request
    assert stat.S_IMODE(data.stat().st_mode) == 0o755


def test_group_writable_data_root_refuses(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch, 0o775)
    host = GenericPackHost(pack_roots=[])
    _refused(lambda: host._allocate_attempt_root("task", "attempt"), data, "group/world-writable")
    assert not (data / "attempts").exists()


def test_foreign_owned_data_root_refuses(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch)
    _metadata_owner(monkeypatch, data, os.getuid() + 1)
    host = GenericPackHost(pack_roots=[])
    _refused(lambda: host._allocate_attempt_root("task", "attempt"), data, "owner uid")
    assert not (data / "attempts").exists()


@pytest.mark.parametrize("component", ["attempts", "executor"])
@pytest.mark.parametrize("mode", [0o755, 0o777])
def test_owned_spool_is_tightened_on_checked_descriptor(tmp_path, monkeypatch, component, mode):
    data = _root(tmp_path, monkeypatch, 0o700)
    host = GenericPackHost(pack_roots=[])
    base = host._default_attempt_base()
    target = data / "attempts" if component == "attempts" else base
    target.chmod(mode)
    identity = target.stat()
    original_fchmod, original_fstat = os.fchmod, os.fstat
    checked, changed, confirmed = set(), [], []

    def fstat(fd):
        metadata = original_fstat(fd)
        if (metadata.st_dev, metadata.st_ino) == (identity.st_dev, identity.st_ino):
            checked.add(fd)
            if changed:
                confirmed.append(stat.S_IMODE(metadata.st_mode))
        return metadata

    def fchmod(fd, permissions):
        assert fd in checked
        metadata = original_fstat(fd)
        assert metadata.st_uid == os.getuid()
        assert (metadata.st_dev, metadata.st_ino) == (identity.st_dev, identity.st_ino)
        changed.append((fd, permissions))
        original_fchmod(fd, permissions)

    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "fchmod", fchmod)
    assert host._allocate_attempt_root("task", "attempt").parent == base
    assert len(changed) == 1 and changed[0][1] == 0o700
    assert confirmed and all(mode == 0o700 for mode in confirmed)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.parametrize("component", ["attempts", "executor"])
def test_foreign_owned_spool_is_never_tightened(tmp_path, monkeypatch, component):
    data = _root(tmp_path, monkeypatch, 0o700)
    host = GenericPackHost(pack_roots=[])
    base = host._default_attempt_base()
    target = data / "attempts" if component == "attempts" else base
    target.chmod(0o755)
    _metadata_owner(monkeypatch, target, os.getuid() + 1)
    monkeypatch.setattr(os, "fchmod", lambda *a: pytest.fail("chmod of a foreign directory"))
    _refused(lambda: host._allocate_attempt_root("task", "attempt"), target, "owner uid")
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


@pytest.mark.parametrize("swap", ["directory", "symlink"])
@pytest.mark.parametrize("component", ["attempts", "executor"])
def test_swap_during_tightening_cannot_chmod_replacement(tmp_path, monkeypatch, swap, component):
    data = _root(tmp_path, monkeypatch, 0o700)
    host = GenericPackHost(pack_roots=[])
    base = host._default_attempt_base()
    target = data / "attempts" if component == "attempts" else base
    target.chmod(0o755)
    identity = target.stat()
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o755)
    replacement.chmod(0o755)
    displaced = target.with_name(target.name + "-displaced")
    original_fchmod = os.fchmod
    calls = []

    def fchmod(fd, mode):
        opened = os.fstat(fd)
        assert (opened.st_dev, opened.st_ino) == (identity.st_dev, identity.st_ino)
        target.rename(displaced)
        if swap == "symlink":
            target.symlink_to(replacement, target_is_directory=True)
        else:
            replacement.rename(target)
        original_fchmod(fd, mode)
        calls.append(fd)

    monkeypatch.setattr(os, "fchmod", fchmod)
    _refused(lambda: host._allocate_attempt_root("task", "attempt"), target, "swapped identity")
    assert len(calls) == 1
    assert stat.S_IMODE(displaced.stat().st_mode) == 0o700
    assert stat.S_IMODE((replacement if swap == "symlink" else target).stat().st_mode) == 0o755
    assert list((replacement if swap == "symlink" else target).iterdir()) == []


def test_tightening_requires_post_chmod_fstat_mode_confirmation(tmp_path, monkeypatch):
    _root(tmp_path, monkeypatch, 0o700)
    host = GenericPackHost(pack_roots=[])
    base = host._default_attempt_base()
    base.chmod(0o755)
    monkeypatch.setattr(os, "fchmod", lambda fd, mode: None)
    _refused(lambda: host._allocate_attempt_root("task", "attempt"), base, "expected 0700")
    assert stat.S_IMODE(base.stat().st_mode) == 0o755


def test_root_owned_sticky_and_0755_parents_are_accepted(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o1777)
    shared.chmod(0o1777)
    parent = shared / "parent"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    data = parent / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(data))
    # Simulate root-owned parents independently of the uid running this test.
    _metadata_owner(monkeypatch, shared, 0)
    _metadata_owner(monkeypatch, parent, 0)
    host = GenericPackHost(pack_roots=[])
    assert host._allocate_attempt_root("task", "attempt").is_dir()
    assert stat.S_IMODE(shared.stat().st_mode) == 0o1777
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(data.stat().st_mode) == 0o755


@pytest.mark.parametrize("selection", ["data", "xdg", "home"])
def test_existing_0755_selected_root_is_preserved(tmp_path, monkeypatch, selection):
    if selection == "home":
        root = Path.home() / ".cache" / "astrid"
        root.mkdir(parents=True, mode=0o755)
    else:
        root = tmp_path / selection
        root.mkdir(mode=0o755)
        monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT" if selection == "data" else "XDG_RUNTIME_DIR", str(root))
    root.chmod(0o755)
    host = GenericPackHost(pack_roots=[])
    attempt = host._allocate_attempt_root("task", "attempt")
    assert attempt.parent.parent.parent == root
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    for directory in (root / "attempts", attempt.parent, attempt):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_ancestor_symlink_swap_during_tightening_refuses(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch, 0o700)
    host = GenericPackHost(pack_roots=[])
    base = host._default_attempt_base()
    base.chmod(0o755)
    moved = data.with_name("moved-data")
    original_fchmod = os.fchmod
    calls = []

    def fchmod(fd, mode):
        data.rename(moved)
        data.symlink_to(moved, target_is_directory=True)
        original_fchmod(fd, mode)
        calls.append(fd)

    monkeypatch.setattr(os, "fchmod", fchmod)
    _refused(host._default_attempt_base, data, "swapped identity")
    assert len(calls) == 1
    assert data.is_symlink()
    assert stat.S_IMODE(moved.stat().st_mode) == 0o700
