"""VibeComfy's runtime folder: unchanged on POSIX, short and cleaned up on Windows."""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path, PureWindowsPath

import pytest

from astrid.core.generation import vibecomfy_runtime_root as module
from astrid.core.generation.vibecomfy_runtime_root import (
    remove_windows_runtime_root,
    vibecomfy_runtime_root,
)

ROOT = Path(__file__).resolve().parents[3]
ATTEMPT = "astrid-attempt-" + "a" * 32


def _windows_destination(user: str = "u" * 40):
    return PureWindowsPath(f"C:/Users/{user}/AppData/Local/Astrid Runtime/attempts/{'b' * 16}/{ATTEMPT}/outputs")


@pytest.mark.parametrize("platform_name", ["linux", "darwin"])
def test_posix_keeps_the_runtime_folder_inside_the_outputs(tmp_path, platform_name):
    assert vibecomfy_runtime_root(tmp_path, platform_name=platform_name) == tmp_path / ".vibecomfy-runtime"
    assert vibecomfy_runtime_root(tmp_path, platform_name=platform_name).parts == (*tmp_path.parts, ".vibecomfy-runtime")


@pytest.mark.posix_native
def test_posix_host_defaults_to_the_runtime_folder_inside_the_outputs(tmp_path):
    assert vibecomfy_runtime_root(tmp_path) == tmp_path / ".vibecomfy-runtime"
    for platform_name in ("linux", "darwin"):
        assert str(vibecomfy_runtime_root(tmp_path, platform_name=platform_name)) == str(tmp_path) + "/.vibecomfy-runtime"


def test_default_platform_is_the_host_platform(tmp_path):
    destination = tmp_path / "attempts" / ("b" * 16) / ATTEMPT / "outputs"
    if sys.platform == "win32":
        key = hashlib.sha256(ATTEMPT.encode()).hexdigest()[:16]
        expected = tmp_path.resolve() / "vr" / key
    else:
        expected = destination / ".vibecomfy-runtime"
    assert vibecomfy_runtime_root(destination) == expected


def test_windows_uses_a_short_directory_under_the_data_root():
    root = vibecomfy_runtime_root(_windows_destination(), platform_name="win32")
    key = hashlib.sha256(ATTEMPT.encode()).hexdigest()[:16]
    assert root == PureWindowsPath("C:/Users/" + "u" * 40 + "/AppData/Local/Astrid Runtime/vr") / key
    assert vibecomfy_runtime_root(_windows_destination(), platform_name="win32") == root  # host and child agree
    other = PureWindowsPath(str(_windows_destination()).replace("a" * 32, "c" * 32))
    assert vibecomfy_runtime_root(other, platform_name="win32") != root  # one folder per attempt


def test_windows_layout_without_an_attempts_directory_stays_beside_the_attempt():
    destination = PureWindowsPath("D:/spool/task-1-attempt-1/outputs")
    key = hashlib.sha256(b"task-1-attempt-1").hexdigest()[:16]
    assert vibecomfy_runtime_root(destination, platform_name="win32") == PureWindowsPath("D:/spool/vr") / key


def test_deepest_vibecomfy_paths_fit_without_long_paths_for_a_40_character_user():
    # Names VibeComfy writes: out/runs/<run>/transactions/<api_digest>/<file>. The run id is the longest
    # of VibeComfy's own (run-<10 digits>-<8 hex>) and Astrid's native one (run-<uuid hex>).
    root = vibecomfy_runtime_root(_windows_destination(), platform_name="win32")
    run = root / "out" / "runs" / ("run-" + "d" * 32)
    transactions = run / "transactions" / ("e" * 64)
    files = [transactions / "canvas_verified.json.tmp", transactions / "lifecycle_events.jsonl",
             run / "managed-generation-result.json.tmp"]
    assert max(len(str(path)) for path in files) < 260
    assert len(str(transactions)) < 248
    assert max(len(str(path)) for path in (run, root / "out" / "runs", root)) < 248
    # The old layout is what failed: the same file under the outputs directory.
    old = _windows_destination() / ".vibecomfy-runtime" / "out" / "runs" / ("run-" + "d" * 32)
    assert len(str(old / "transactions" / ("e" * 64))) >= 248


def test_every_call_site_gets_its_location_from_the_helper():
    literal = re.compile(r"""['"]\.vibecomfy-runtime""")
    offenders = [str(path.relative_to(ROOT)) for path in (ROOT / "astrid").rglob("*.py")
                 if path != Path(module.__file__) and literal.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
    for relative in ("astrid/packs/vibecomfy/local_server_engine.py",
                     "astrid/core/generation/backends/vibecomfy_local_server.py",
                     "astrid/core/generation/producer_result.py"):
        assert "vibecomfy_runtime_root(destination)" in (ROOT / relative).read_text(encoding="utf-8")


def test_cleanup_removes_the_windows_runtime_directory(tmp_path, monkeypatch):
    attempt = tmp_path / "attempts" / ("b" * 16) / ATTEMPT
    (attempt / "outputs").mkdir(parents=True)
    monkeypatch.setattr(module.sys, "platform", "win32")
    # Path.resolve on a POSIX host keeps this layout; the helper then names <tmp>/vr/<key>.
    short = Path(vibecomfy_runtime_root(attempt / "outputs"))
    assert short.parent == tmp_path.resolve() / "vr"
    (short / "out" / "runs" / "run-1" / "transactions" / ("e" * 64)).mkdir(parents=True)
    remove_windows_runtime_root(attempt / "outputs")
    assert not short.exists()
    remove_windows_runtime_root(attempt / "outputs")  # never created, or already gone


def test_cleanup_does_nothing_off_windows(tmp_path):
    runtime = tmp_path / ".vibecomfy-runtime"
    runtime.mkdir()
    remove_windows_runtime_root(tmp_path)
    assert runtime.is_dir()


def test_host_attempt_cleanup_also_removes_the_windows_runtime_directory(tmp_path, monkeypatch):
    from astrid.core.execution import generic_host

    attempt = tmp_path / "attempts" / ("b" * 16) / ATTEMPT
    (attempt / "outputs").mkdir(parents=True)
    monkeypatch.setattr(module.sys, "platform", "win32")
    short = Path(vibecomfy_runtime_root(attempt / "outputs"))
    short.mkdir(parents=True)
    generic_host._cleanup_ephemeral_attempt(attempt)
    assert not attempt.exists() and not short.exists()
