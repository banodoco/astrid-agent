"""CPU simulations of the Windows controller boundary; no provider is contacted."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import failure_diagnostics


def _controller_process(source):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ready_controller_without_fcntl_keeps_one_exec_zero_process_budget():
    _controller_process("""
        import sys
        from types import SimpleNamespace
        sys.modules['fcntl'] = None
        from astrid.packs.runpod.machine import ensure_pod_ready, pod_paths, pod_state_command
        from tests.packs.runpod.test_remote_placement import FakePod

        paths = pod_paths()
        assert paths == {
            'root': '/root/astrid', 'data_root': '/root/astrid/runtime-data',
            'source_manifest': '/root/astrid/source-manifest.json',
            'ready': '/root/astrid/ready.json', 'prepared': '/root/astrid/prepared-connection.json',
            'active': '/root/astrid/active-job.json', 'lock': '/root/astrid/reset.lock',
            'prepared_env': 'ASTRID_PREPARED_CONNECTION',
        }
        command = pod_state_command()
        pod = FakePod({}, 'test-only')
        pod.pod_state = 'ready'
        def execute(state, actual, *, timeout):
            assert timeout == 60
            return pod.exec(actual, timeout=timeout)
        def start(*args, **kwargs):
            raise AssertionError('a ready pod must not start a reset process')
        placement = SimpleNamespace(_exec=execute, _run_job=start)
        state = SimpleNamespace(note=lambda *args, **kwargs: None)
        assert ensure_pod_ready(placement, state) == {
            'entry': None, 'ready': True, 'found': 'ready', 'reset': 'skipped',
        }
        assert pod.exec_log == [command]
        assert pod.job_log == []
        assert pod.put_log == []
    """)


def test_claim_job_release_controller_imports_without_posix_modules():
    _controller_process("""
        import importlib
        import sys
        for name in ('fcntl', 'pwd', 'grp', 'termios'):
            sys.modules[name] = None
        modules = (
            'machine', 'remote_placement', 'worker_owner', 'worker_preparation',
            'target_selection', 'provider', 'release_control', 'failure_diagnostics',
            'failure_receipt', 'generation', 'adapter', 'pod_watchdog', 'engine_snapshot',
            'worker_process', 'worker_staging', 'worker_session', 'installed_session',
            'managed_sources', 'prepared_task', 'reuse_policy', 'pod_reset',
        )
        for name in modules:
            importlib.import_module('astrid.packs.runpod.' + name)
        from astrid.packs.runpod.machine import pod_paths, pod_state_command
        assert pod_paths()['lock'] == '/root/astrid/reset.lock'
        assert 'pod-state:' in pod_state_command()
    """)


@pytest.mark.parametrize('symlink', [False, True], ids=['regular-file', 'symlink'])
def test_graph_facts_without_os_o_nofollow(tmp_path, monkeypatch, symlink):
    source = tmp_path / 'source.json'
    source.write_text('{"17":{"class_type":"KSampler","inputs":{"seed":42}}}')
    path = source
    if symlink:
        path = tmp_path / 'linked.json'
        path.symlink_to(source)
    # On POSIX filesystem is os itself: freeze its platform capability before
    # removing the stdlib name to simulate the controller's Windows environment.
    from runtime_protocol.dirfd import filesystem
    portable = SimpleNamespace(
        open=filesystem.open, O_RDONLY=filesystem.O_RDONLY,
        O_NOFOLLOW=filesystem.O_NOFOLLOW, O_NONBLOCK=filesystem.O_NONBLOCK,
    )
    monkeypatch.setattr(failure_diagnostics, 'filesystem', portable, raising=False)
    monkeypatch.delattr(os, 'O_NOFOLLOW')
    expected = (frozenset(), frozenset()) if symlink else (
        frozenset({'17'}), frozenset({'KSampler', 'seed'}),
    )
    assert failure_diagnostics.graph_facts(path) == expected
