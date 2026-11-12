"""The thread guard reports leaks and optionally prevents interpreter shutdown hangs."""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytest_plugins = ["pytester"]
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def run(pytester):
    pytester.makeconftest('''
from tests.lanes import _ThreadGuard

def pytest_configure(config):
    config.pluginmanager.register(_ThreadGuard(), "thread-guard")
''')

    def go(source, opt_in=False):
        pytester.makepyfile(test_case=source)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_CURRENT_TEST", None)
        env.pop("ASTRID_PYTEST_HARD_EXIT_ON_LEAKED_THREADS", None)
        if opt_in:
            env["ASTRID_PYTEST_HARD_EXIT_ON_LEAKED_THREADS"] = "1"
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-rA"],
            cwd=pytester.path, env=env, capture_output=True, text=True, timeout=10,
        )
        return result, time.monotonic() - started

    return go


@pytest.mark.parametrize("failure,code,summary", [(False, 0, "PASSED"), (True, 1, "FAILED")])
def test_opt_in_exits_with_pytest_status_and_full_summary(run, failure, code, summary):
    result, elapsed = run(f'''
import threading

def worker_target():
    threading.Event().wait()

def test_leak():
    threading.Thread(target=worker_target, name="visible-leak").start()
    assert {not failure}
''', opt_in=True)
    assert result.returncode == code
    assert elapsed < 8
    assert f"{summary} test_case.py::test_leak" in result.stdout
    assert "visible-leak" in result.stdout
    assert "worker_target" in result.stdout
    assert "threading.Event().wait()" in result.stdout
    assert result.stdout.rfind("threading.Event().wait()") > result.stdout.rfind(f"{summary} test_case.py::test_leak")


def test_without_opt_in_reports_but_waits_for_thread(run):
    result, elapsed = run('''
import threading
import time

def worker_target():
    time.sleep(2)

def test_leak():
    threading.Thread(target=worker_target, name="temporary-leak").start()
''')
    assert result.returncode == 0
    assert elapsed >= 2
    assert "PASSED test_case.py::test_leak" in result.stdout
    assert "temporary-leak" in result.stdout
    assert "worker_target" in result.stdout
    assert "time.sleep(2)" in result.stdout


def test_clean_session_and_daemon_thread_pass(run):
    result, _ = run('''
import threading

def test_clean():
    threading.Thread(target=threading.Event().wait, name="daemon-ok", daemon=True).start()
''', opt_in=True)
    assert result.returncode == 0
    assert "leaked non-daemon threads" not in result.stdout
    assert "daemon-ok" not in result.stdout


def test_inline_pytest_does_not_hard_exit(run):
    result, _ = run('''
import threading
import pytest
from tests.lanes import _ThreadGuard

def test_inline(tmp_path):
    inner = tmp_path / "test_inner.py"
    inner.write_text("def test_inner(): pass\\n")
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="inline-leak")
    thread.start()
    try:
        assert pytest.main([str(inner), "-p", "no:cacheprovider", "-o", "addopts="],
                           plugins=[_ThreadGuard()]) == 0
        print("inline returned")
    finally:
        release.set()
        thread.join()
''', opt_in=True)
    assert result.returncode == 0
    assert "PASSED test_case.py::test_inline" in result.stdout
    assert "inline returned" in result.stdout
