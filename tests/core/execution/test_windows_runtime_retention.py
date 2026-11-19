"""Short VibeComfy scratch follows kept and crash-abandoned attempt custody."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import vibecomfy_runtime_root as runtime_roots
from astrid.sdk.storage_ownership import reclaim
from tests import test_generic_host_result_json as host_results

host_case = host_results.host_case


@pytest.fixture
def windows_runtime_paths(monkeypatch):
    # Exercise the Windows layout on every runner without changing the host's
    # platform or replacing native filesystem operations on Windows CI.
    monkeypatch.setattr(runtime_roots, "sys", SimpleNamespace(platform="win32"))


def _attempt(data: Path, name: str = "a" * 32) -> tuple[Path, Path]:
    root = data / "attempts" / ("b" * 16) / ("astrid-attempt-" + name)
    (root / "outputs").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    short = Path(runtime_roots.vibecomfy_runtime_root(root / "outputs"))
    short.mkdir(parents=True)
    (short / "runtime.bin").write_bytes(b"temporary runtime")
    return root, short


def _reclaim(data: Path, tmp_path: Path, **kwargs):
    return reclaim(data, temporary_root=tmp_path / "unused-temp",
                   cache_root=tmp_path / "unused-cache", **kwargs)


def test_kept_success_removes_short_runtime_but_preserves_receipt(
    host_case, monkeypatch, windows_runtime_paths,
):
    case = host_case
    host_results._success_boundary(case, monkeypatch)
    execute = case.host._run_command_definition
    short = Path(runtime_roots.vibecomfy_runtime_root(case.root / "outputs"))

    def produce(*args, **kwargs):
        short.mkdir(parents=True)
        (short / "runtime.bin").write_bytes(b"temporary runtime")
        return execute(*args, **kwargs)

    monkeypatch.setattr(case.host, "_run_command_definition", produce)
    result = case.host.run_task(case.task, lease_token="lease-result", keep_attempt=True)
    assert result["task"]["status"] == "completed"
    assert (case.root / "result.json").is_file()
    assert (case.root / "outputs" / "answer.txt").read_bytes() == b"complete output\n"
    assert not short.exists()


def test_crashed_attempt_reclaim_removes_private_attempt_and_short_runtime(tmp_path):
    data = tmp_path / "data"
    locations = tmp_path / "locations.json"
    script = r'''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from astrid.core.generation import vibecomfy_runtime_root as runtime_roots
runtime_roots.sys = SimpleNamespace(platform="win32")
root = Path(sys.argv[1]) / "attempts" / ("b" * 16) / ("astrid-attempt-" + "a" * 32)
(root / "outputs").mkdir(parents=True, mode=0o700)
root.chmod(0o700)
short = Path(runtime_roots.vibecomfy_runtime_root(root / "outputs"))
short.mkdir(parents=True)
(short / "runtime.bin").write_bytes(b"crash residue")
Path(sys.argv[2]).write_text(json.dumps([str(root), str(short)]), encoding="utf-8")
os._exit(23)
'''
    completed = subprocess.run([sys.executable, "-c", script, str(data), str(locations)],
                               capture_output=True, text=True, timeout=15, check=False)
    assert completed.returncode == 23, completed.stderr
    root, short = map(Path, json.loads(locations.read_text(encoding="utf-8")))
    assert root.exists() and short.exists()
    # The process died without a finally; normal retention, not its cleanup,
    # must remove both namespaces. Use the real clock offset, not a sleep.
    from unittest.mock import patch
    with patch.object(runtime_roots, "sys", SimpleNamespace(platform="win32")):
        report = _reclaim(data, tmp_path, now=time.time() + 3 * 86400)
    assert not root.exists() and not short.exists()
    assert report["reclaimed_bytes"] == len(b"crash residue")


def test_retention_dry_run_accounts_for_both_attempt_namespaces(tmp_path, windows_runtime_paths):
    data = tmp_path / "data"
    root, short = _attempt(data)
    (root / "result.json").write_bytes(b"receipt")
    report = _reclaim(data, tmp_path, dry_run=True, now=time.time() + 3 * 86400)
    assert root.exists() and short.exists()
    assert report["reclaimable_bytes"] == len(b"receipt") + len(b"temporary runtime")
    assert report["reclaimed_bytes"] == 0


@pytest.mark.parametrize("marker", ["fail-request.json", "cancel-request.json", "pending.reconciliation.json"])
def test_reconciliation_retains_short_runtime_with_its_attempt(tmp_path, windows_runtime_paths, marker):
    data = tmp_path / "data"
    root, short = _attempt(data)
    (root / marker).write_bytes(b"unresolved evidence")
    report = _reclaim(data, tmp_path, now=time.time() + 3 * 86400)
    assert root.exists() and (short / "runtime.bin").read_bytes() == b"temporary runtime"
    assert report["reclaimed_bytes"] == 0


def test_failed_short_runtime_delete_keeps_attempt_for_retry(tmp_path, monkeypatch, windows_runtime_paths):
    data = tmp_path / "data"
    root, short = _attempt(data)
    original = runtime_roots.shutil.rmtree

    def blocked(path, *args, **kwargs):
        if Path(path) == short:
            raise PermissionError("runtime file still open")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(runtime_roots.shutil, "rmtree", blocked)
    report = _reclaim(data, tmp_path, now=time.time() + 3 * 86400)
    assert root.exists() and short.exists()
    assert report["reclaimed_bytes"] == 0
    monkeypatch.setattr(runtime_roots.shutil, "rmtree", original)
    _reclaim(data, tmp_path, now=time.time() + 3 * 86400)
    assert not root.exists() and not short.exists()


def test_reclaim_keeps_fresh_attempt_and_its_short_runtime(tmp_path, windows_runtime_paths):
    data = tmp_path / "data"
    root, short = _attempt(data)
    _reclaim(data, tmp_path)
    assert root.exists() and short.exists()


def test_legacy_temp_reclaim_also_removes_short_runtime(tmp_path, windows_runtime_paths):
    temporary = tmp_path / "temp"
    root = temporary / ("astrid-attempt-" + "c" * 32)
    root.mkdir(parents=True, mode=0o700)
    short = Path(runtime_roots.vibecomfy_runtime_root(root / "outputs"))
    short.mkdir(parents=True)
    (short / "runtime.bin").write_bytes(b"legacy runtime")
    reclaim(tmp_path / "data", temporary_root=temporary, cache_root=tmp_path / "cache",
            now=time.time() + 3 * 86400)
    assert not root.exists() and not short.exists()


def test_linked_short_root_parent_cannot_redirect_cleanup(tmp_path, windows_runtime_paths):
    data = tmp_path / "data"
    root, short = _attempt(data)
    outside = tmp_path / "outside"
    short.parent.rename(outside)
    if os.name == "nt":
        completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(short.parent), str(outside)],
                                   capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stdout + completed.stderr
    else:
        short.parent.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(OSError, match="linked VibeComfy"):
            runtime_roots.remove_windows_runtime_root(root / "outputs")
        report = _reclaim(data, tmp_path, now=time.time() + 3 * 86400)
        assert root.exists()
        assert (outside / short.name / "runtime.bin").read_bytes() == b"temporary runtime"
        assert report["reclaimed_bytes"] == 0
    finally:
        if os.name == "nt":
            short.parent.rmdir()
        else:
            short.parent.unlink()


@pytest.mark.parametrize("disposition", ["missing-child", "no-op"])
def test_short_runtime_cleanup_verifies_absence(tmp_path, monkeypatch, windows_runtime_paths, disposition):
    root, short = _attempt(tmp_path / "data")

    def incomplete(_path):
        if disposition == "missing-child":
            raise FileNotFoundError("a nested entry disappeared")

    monkeypatch.setattr(runtime_roots.shutil, "rmtree", incomplete)
    with pytest.raises(OSError, match="cleanup was not verified"):
        runtime_roots.remove_windows_runtime_root(root / "outputs")
    assert short.exists()
