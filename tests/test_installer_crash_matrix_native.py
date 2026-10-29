"""Four native filesystem samples; service ownership remains a fixture boundary.

Run with ``-m crash_matrix`` on the named native host. Linux's exhaustive
SIGKILL matrix lives in test_installer_update_recovery.py. Service manager effects remain fixture boundaries. After retry, the owned
loopback Runtime fixture verifies one healthy owner and refuses a second owner.
"""
from __future__ import annotations

import pytest

from astrid.installer.core import current, read_json
from tests.test_installer_update_recovery import _assert_one_runtime, _run, _setup

pytestmark = [pytest.mark.integration, pytest.mark.crash_matrix]
POINTS = ["staged", "migrated", "switched", "rollback_selected"]


def _sample(base, monkeypatch, real_runtime, point):
    monkeypatch.setenv("ASTRID_TEST_CRASH_METHOD", "exit")
    _setup(base)
    if point == "rollback_selected":
        (base / "reject-candidate").touch()
    interrupted = _run(base, point)
    assert interrupted.returncode == 86, interrupted.stderr
    (base / "reject-candidate").unlink(missing_ok=True)
    repaired = _run(base, mode="install")
    assert repaired.returncode == 0, repaired.stderr
    selected = current(base / "program")
    assert selected.root.name == "old"
    assert read_json(base / "install-selection.json")["identity"] == selected.document()["identity"]
    assert (base / "data/retained.txt").read_text() == "original user data"
    completed = _run(base)
    assert completed.returncode == 0, completed.stderr
    assert read_json(base / "program/update-transaction.json")["step"] == "complete"
    _assert_one_runtime(base, real_runtime)


@pytest.mark.darwin_native
@pytest.mark.parametrize("point", POINTS)
def test_macos_crash_matrix_sample(tmp_path, monkeypatch, real_runtime, point):
    _sample(tmp_path, monkeypatch, real_runtime, point)


@pytest.mark.windows_native
@pytest.mark.parametrize("point", POINTS)
def test_windows_crash_matrix_sample(tmp_path, monkeypatch, real_runtime, point):
    _sample(tmp_path, monkeypatch, real_runtime, point)


@pytest.mark.windows_native
def test_windows_locked_realm_refuses_restore_then_recovers(tmp_path, monkeypatch, real_runtime):
    """A native handle denies delete sharing during the restore rename."""
    import ctypes
    from ctypes import wintypes

    monkeypatch.setenv("ASTRID_TEST_CRASH_METHOD", "exit")
    _setup(tmp_path)
    assert _run(tmp_path, "runtime:stopped").returncode == 86
    transaction = read_json(tmp_path / "program/update-transaction.json")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    realm = tmp_path / "data/runtime/realms/realm-1"
    handle = kernel.CreateFileW(str(realm), 0x80000000, 0x1 | 0x2, None, 3, 0x02000000, None)
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    try:
        refused = _run(tmp_path, mode="install")
        assert refused.returncode != 0
        pending = read_json(tmp_path / "program/update-transaction.json")
        assert pending["backup"] == transaction["backup"]
        assert pending["operation_id"] == transaction["operation_id"]
        assert pending["step"] == "recovering"
    finally:
        assert kernel.CloseHandle(handle)
    repaired = _run(tmp_path, mode="install")
    assert repaired.returncode == 0, repaired.stderr
    assert current(tmp_path / "program").root.name == "old"
    assert (tmp_path / "data/retained.txt").read_text() == "original user data"
    completed = _run(tmp_path)
    assert completed.returncode == 0, completed.stderr
    _assert_one_runtime(tmp_path, real_runtime)
