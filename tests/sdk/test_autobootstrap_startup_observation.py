from __future__ import annotations

import base64
import hashlib
import itertools
import json
import multiprocessing
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_protocol import platform

from astrid.sdk import autobootstrap, lock_wait


OPERATION = "a" * 32
SUCCESSOR = "b" * 32


def _operation(operation_id=OPERATION, *, state="pending"):
    return {"operation_id": operation_id, "phase": "checking_objects", "state": state, "objects_checked": 8}


@pytest.fixture
def installed_launcher(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("the direct installed console script fixture requires POSIX")

    def select():
        environment = tmp_path / "installed" / "py"
        scripts = environment / "bin"
        scripts.mkdir(parents=True)
        python = scripts / "python"
        python.symlink_to(sys.executable)
        launcher = scripts / "astrid-runtime"
        source = (f"#!{python}\nimport sys\nfrom astrid_runtime.cli import main\n"
                  'if __name__ == "__main__":\n    sys.exit(main())\n').encode()
        launcher.write_bytes(source)
        launcher.chmod(0o755)
        packages = environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        dist = packages / "astrid_runtime-0.1.0.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text("Name: astrid-runtime\nVersion: 0.1.0\n")
        (dist / "entry_points.txt").write_text("[console_scripts]\nastrid-runtime = astrid_runtime.cli:main\n")
        digest = base64.urlsafe_b64encode(hashlib.sha256(source).digest()).decode().rstrip("=")
        (dist / "RECORD").write_text(f"../../../bin/astrid-runtime,sha256={digest},{len(source)}\n")
        monkeypatch.syspath_prepend(str(packages))
        monkeypatch.setattr(sys, "executable", str(python))
        monkeypatch.setattr(sys, "prefix", str(environment))
        monkeypatch.setenv("PATH", str(scripts))
        monkeypatch.delenv("PYTHONPATH", raising=False)
        monkeypatch.delenv("PYTHONHOME", raising=False)
        monkeypatch.delenv("ASTRID_RUNTIME_LAUNCHER", raising=False)
        manifest = tmp_path / "installed" / "profile.json"
        manifest.write_text(json.dumps({"profile": "astrid", "runtime_environment": str(environment)}))
        monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(manifest))
        return launcher, manifest

    return select


def _hold_file(path, ready, release):
    with Path(path).open("a+") as handle:
        platform.lock_file(handle.fileno())
        ready.set()
        assert release.wait(10)


@contextmanager
def _held_acquisition(data_root, kind, monkeypatch):
    if kind == "thread":
        ready, release = threading.Event(), threading.Event()

        def hold():
            with autobootstrap._ACQUISITION_LOCK:
                ready.set()
                assert release.wait(10)

        holder = threading.Thread(target=hold)
        monkeypatch.setattr(lock_wait, "LOCK_TIMEOUT", 0.0)
    else:
        lock_path = autobootstrap._acquisition_lock_path(data_root)
        lock_path.parent.mkdir(parents=True)
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        holder = context.Process(target=_hold_file, args=(str(lock_path), ready, release))
        monkeypatch.delenv("ASTRID_RUNTIME_ADMISSION_TIMEOUT_SECONDS", raising=False)
        clock = itertools.chain((0.0,), itertools.repeat(135.0))
        monkeypatch.setattr(autobootstrap, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    holder.start()
    try:
        assert ready.wait(5)
        yield
        assert holder.is_alive()
        if kind == "thread":
            assert not autobootstrap._ACQUISITION_LOCK.acquire(blocking=False)
        else:
            with lock_path.open("a+") as handle, pytest.raises(BlockingIOError):
                platform.lock_file(handle.fileno(), blocking=False)
    finally:
        release.set()
        holder.join(5)
        if kind == "file" and holder.is_alive():
            holder.terminate()
            holder.join(5)
    assert not holder.is_alive()
    if kind == "file":
        assert holder.exitcode == 0


def _status_replies(monkeypatch, *replies):
    calls = []
    responses = iter(replies)

    def run(command, **kwargs):
        assert command[1] == "startup-status"
        assert 0 < kwargs["timeout"] <= 5.0
        assert kwargs["stdin"] == subprocess.DEVNULL
        calls.append(command)
        returncode, value = next(responses)
        return subprocess.CompletedProcess(command, returncode, json.dumps(value), "")

    monkeypatch.setattr(autobootstrap.subprocess, "run", run)
    return calls


def _assert_probes(calls, launcher, data_root, manifest, *, exact):
    first = [str(launcher), "startup-status", "--data-root", str(data_root), "--require-configured-selection",
             "--source-manifest", str(manifest)]
    assert calls == ([first + ["--json"], first + ["--operation-id", OPERATION, "--json"]]
                     if exact else [first + ["--json"]])


@pytest.mark.parametrize("lock", ["thread", "file"])
def test_held_acquisition_reports_exact_pending_operation(tmp_path, monkeypatch, installed_launcher, lock):
    data_root = tmp_path / "support"
    with _held_acquisition(data_root, lock, monkeypatch):
        launcher, manifest = installed_launcher()
        calls = _status_replies(monkeypatch, (0, _operation()), (0, _operation()))
        with pytest.raises(autobootstrap.AutoBootstrapError) as error:
            autobootstrap.ensure_runtime(data_root=data_root)
        assert error.value.code == "runtime_startup_pending"
        assert error.value.details == _operation()
        _assert_probes(calls, launcher, data_root, manifest, exact=True)


@pytest.mark.parametrize("followup", [
    (0, _operation(SUCCESSOR)),
    (1, {"state": "unavailable"}),
    (0, _operation(state="ended")),
    (0, {**_operation(), "objects_checked": True}),
    (0, {"attempt_id": OPERATION, "state": "starting_service"}),
], ids=["different-id", "unavailable-scope", "ended", "malformed", "attempt-instead"])
def test_observed_operation_cannot_be_replaced_or_authorize_acquisition(
    tmp_path, monkeypatch, installed_launcher, followup,
):
    data_root = tmp_path / "support"
    with _held_acquisition(data_root, "thread", monkeypatch):
        launcher, manifest = installed_launcher()
        calls = _status_replies(monkeypatch, (0, _operation()), followup)
        with pytest.raises(autobootstrap.AutoBootstrapError) as error:
            autobootstrap.ensure_runtime(data_root=data_root)
        assert error.value.code == "runtime_acquisition_lock_timeout"
        assert not error.value.details
        _assert_probes(calls, launcher, data_root, manifest, exact=True)


@pytest.mark.parametrize("status", [
    (2, None),
    (1, {"state": "unavailable"}),
    (0, _operation(state="ended")),
    (0, {"attempt_id": OPERATION, "state": "inspecting_workspace"}),
], ids=["unsupported", "unavailable", "ended", "attempt"])
def test_first_status_requires_an_operation_and_never_polls_an_attempt(
    tmp_path, monkeypatch, installed_launcher, status,
):
    data_root = tmp_path / "support"
    with _held_acquisition(data_root, "thread", monkeypatch):
        launcher, manifest = installed_launcher()
        calls = _status_replies(monkeypatch, status)
        with pytest.raises(autobootstrap.AutoBootstrapError) as error:
            autobootstrap.ensure_runtime(data_root=data_root)
        assert error.value.code == "runtime_acquisition_lock_timeout"
        assert not error.value.details
        _assert_probes(calls, launcher, data_root, manifest, exact=False)


@pytest.mark.parametrize("route", ["override", "other-on-path", "modified-console-script"])
def test_unverified_selection_keeps_lock_timeout_without_a_probe(tmp_path, monkeypatch, installed_launcher, route):
    with _held_acquisition(tmp_path / "support", "thread", monkeypatch):
        launcher, _ = installed_launcher()
        if route == "override":
            monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", str(launcher))
        elif route == "other-on-path":
            other = tmp_path / "other" / launcher.name
            other.parent.mkdir()
            other.write_bytes(launcher.read_bytes())
            other.chmod(0o755)
            monkeypatch.setenv("PATH", str(other.parent))
        else:
            with launcher.open("ab") as stream:
                stream.write(b"# not the recorded entry point\n")
        calls = _status_replies(monkeypatch)
        with pytest.raises(autobootstrap.AutoBootstrapError) as error:
            autobootstrap.ensure_runtime(data_root=tmp_path / "support")
        assert error.value.code == "runtime_acquisition_lock_timeout"
        assert not calls
