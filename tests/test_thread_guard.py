"""The session thread guard reports non-daemon threads without waiting for them."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def run(pytester, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    pytester.makeconftest('''
import threading
import pytest
from tests.lanes import _ThreadGuard

release = threading.Event()

def pytest_configure(config):
    config.pluginmanager.register(_ThreadGuard(), "thread-guard")

@pytest.hookimpl(hookwrapper=True)
def pytest_sessionfinish(session, exitstatus):
    yield
    release.set()
''')

    def go(source):
        pytester.makepyfile(test_case=source)
        return pytester.runpytest_subprocess("-p", "no:cacheprovider", "-o", "addopts=")

    return go


def test_live_non_daemon_thread_fails_and_reports_stack(run):
    result = run('''
import threading
from conftest import release

def worker_target():
    release.wait()

def test_leak():
    threading.Thread(target=worker_target, name="visible-leak").start()
''')
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    output = result.stdout.str()
    assert "visible-leak" in output
    assert "worker_target" in output
    assert "release.wait()" in output


def test_clean_session_and_daemon_thread_pass(run):
    result = run('''
import threading
from conftest import release

def test_clean():
    threading.Thread(target=release.wait, name="daemon-ok", daemon=True).start()
''')
    assert result.ret == pytest.ExitCode.OK
    assert "daemon-ok" not in result.stdout.str()
