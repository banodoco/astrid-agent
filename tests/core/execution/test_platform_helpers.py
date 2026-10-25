from __future__ import annotations

import errno
import os
import sys
import subprocess
from types import SimpleNamespace

import pytest

from runtime_protocol import platform
from astrid.core.execution import custody_broker, platform_helpers, process_group, task_scope


def test_windows_owner_check_uses_handle_not_stat_uid(monkeypatch):
    from contextlib import contextmanager
    from runtime_protocol import windows_security

    monkeypatch.setattr(platform_helpers, "sys", SimpleNamespace(platform="win32"))
    seen = []
    monkeypatch.setattr(platform, "is_owner_only", lambda fd: seen.append(fd) or fd == 123)
    @contextmanager
    def retained(fd):
        yield fd, None
    def validate(fd, *, owner_only):
        assert owner_only is True
        seen.append(fd)
        if fd != 123:
            raise PermissionError("foreign SID")
    monkeypatch.setattr(windows_security, "_handle", retained)
    monkeypatch.setattr(windows_security, "_validate_handle", validate)
    metadata = SimpleNamespace(st_uid=0, st_mode=0o666)
    assert platform_helpers.owned_by_current_user(123, metadata, mode=0o600)
    assert not platform_helpers.owned_by_current_user(124, metadata)
    assert not platform_helpers.owned_by_current_user(None, metadata)
    assert platform_helpers.owned_by_current_user(123, metadata)
    assert seen == [123, 124, 123]


@pytest.mark.parametrize("mode, expected", [(0o600, True), (0o640, False), (0o666, False)])
def test_posix_owner_check_preserves_permissions(monkeypatch, mode, expected):
    monkeypatch.setattr(platform_helpers, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(platform, "is_current_user", lambda info: info.st_uid == 7)
    metadata = SimpleNamespace(st_uid=7, st_mode=mode)
    assert platform_helpers.owned_by_current_user(123, metadata, mode=0o600) is expected
    metadata.st_uid = 8
    assert not platform_helpers.owned_by_current_user(123, metadata, mode=mode)


def test_scope_spawn_delegates_to_runtime(monkeypatch):
    class Child:
        pid = 123
    process = Child()
    seen = []
    monkeypatch.setattr(task_scope, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "spawn_tree", lambda argv, **kw: seen.append((argv, kw)) or process)
    assert task_scope.spawn(["worker"], cwd="attempt") is process
    assert seen == [(["worker"], {"cwd": "attempt", "start_new_session": True})]
    assert process._astrid_task_scope


def test_windows_task_stop_closes_job_without_posix_census(monkeypatch):
    process = SimpleNamespace(pid=123, _runtime_job=object())
    seen = []
    monkeypatch.setattr(task_scope, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "stop_tree", lambda child, **kw: seen.append((child, kw)))
    monkeypatch.setattr(task_scope, "_scope_census", lambda *_: pytest.fail("POSIX census on Windows"))
    assert task_scope.stop(process, grace=0.25).status == "settled"
    assert seen == [(process, {"grace": 0.25})]


@pytest.mark.parametrize("configured_base", [False, True])
def test_windows_attempt_directory_is_private_from_creation(tmp_path, monkeypatch, configured_base):
    from astrid.core.execution import generic_host
    host = object.__new__(generic_host.GenericPackHost)
    host.attempt_base = tmp_path / "attempts" if configured_base else None
    host.attempt_root = None
    monkeypatch.setattr(generic_host, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    created = []
    def private(path):
        created.append(path)
        path.mkdir()
    monkeypatch.setattr(platform, "private_directory", private)
    root = host._allocate_attempt_root("task", "attempt")
    assert root.exists() and created == [root]
    assert root.parent == (host.attempt_base or tmp_path)


def test_windows_task_stop_reports_job_failure(monkeypatch):
    monkeypatch.setattr(task_scope, "sys", SimpleNamespace(platform="win32"))
    def fail(*args, **kwargs):
        raise OSError("job termination failed")
    monkeypatch.setattr(platform, "stop_tree", fail)
    report = task_scope.stop(SimpleNamespace(pid=123, _runtime_job=object()))
    assert report.status == "unresolved"
    assert "job termination failed" in report.detail


@pytest.mark.parametrize("failure", ["still-live", "observation-error"])
def test_windows_orphan_recovery_preserves_unresolved_obligations(tmp_path, monkeypatch, failure):
    import json
    journal = tmp_path / "scopes.json"
    entry = {"pid": 123, "pgid": 123, "start": "windows-creation:7", "task": "attempt"}
    journal.write_text(json.dumps([entry]))
    monkeypatch.setattr(task_scope, "sys", SimpleNamespace(platform="win32"))
    ticks = iter([0.0, 4.0])
    monkeypatch.setattr(task_scope.time, "monotonic", lambda: next(ticks))
    def identity(pid):
        assert pid == 123
        if failure == "observation-error":
            raise PermissionError("identity denied")
        return {"birth_id": entry["start"]}
    monkeypatch.setattr(platform, "process_identity", identity)
    monkeypatch.setattr(platform, "signal_process", lambda *_: pytest.fail("journal granted signal authority"))
    notes = task_scope.reap_orphans(journal)
    assert len(notes) == 1
    assert ("still live" if failure == "still-live" else "unchecked") in notes[0]
    assert json.loads(journal.read_text()) == [entry]


def test_isolated_custody_wrapper_imports_runtime_without_site_initialization():
    environment = dict(os.environ)
    environment.pop("ASTRID_RUNTIME_CUSTODY_TARGET_B64", None)
    result = subprocess.run(custody_broker.custody_wrapper_argv(sys.executable),
                            env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "custody target argv is invalid" in result.stderr


def test_windows_owned_launch_retains_job_and_bypasses_pidfd(monkeypatch, tmp_path):
    monkeypatch.setattr(process_group, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(custody_broker, "sys", SimpleNamespace(platform="win32"))
    owner = custody_broker.ProcessCustody(scope_root=tmp_path)
    monkeypatch.setattr(process_group, "ProcessCustody", lambda: owner)
    process = SimpleNamespace(pid=123, _runtime_job=object())
    seen = []
    monkeypatch.setattr(platform, "spawn_tree", lambda argv, **kw: seen.append((argv, kw)) or process)
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"birth_id": "windows-creation:7"})
    monkeypatch.setattr(owner, "start_observer", lambda: pytest.fail("POSIX observer on Windows"))
    assert process_group.popen_owned_group(["worker"], detached=True) is process
    assert process._astrid_custody is owner
    assert owner._handles[123] == ("windows-creation:7", process)
    assert seen[0][1]["detached"] is True
    signals = []
    monkeypatch.setattr(platform, "signal_tree", lambda child, sig: signals.append((child, sig)))
    owner.signal(123, "windows-creation:7", platform.TERM)
    assert signals == [(process, platform.TERM)]
    with pytest.raises(custody_broker.CustodyError, match="exact process capability"):
        owner.signal(123, "replacement", platform.KILL)


def test_windows_cleanup_requires_closed_job(monkeypatch):
    monkeypatch.setattr(process_group, "sys", SimpleNamespace(platform="win32"))
    job = SimpleNamespace(handle=42)
    process = SimpleNamespace(pid=123, _runtime_job=job, poll=lambda: 0)
    with pytest.raises(process_group.CleanupUncertainError, match="unproved"):
        process_group.verify_tree_absent(process)
    del process._astrid_tree_uncertain
    def stop(child, **kwargs):
        assert child is process
        job.handle = None
    monkeypatch.setattr(platform, "stop_tree", stop)
    process_group.terminate_group(process)
    assert job.handle is None


@pytest.fixture
def agent_census_walk(monkeypatch):
    """Off Linux the agent still walks its own census; Linux uses Runtime's walk."""
    monkeypatch.setattr(platform_helpers.sys, "platform", "darwin")


@pytest.mark.parametrize("birth", [None, "old"])
def test_recovered_group_refuses_missing_or_reused_birth(agent_census_walk, monkeypatch, birth):
    monkeypatch.setattr(platform, "process_group", lambda pid: 10)
    monkeypatch.setattr(process_group, "_process_snapshot", lambda: {
        100: SimpleNamespace(birth="new", pgid=100),
    })
    monkeypatch.setattr(platform, "signal_process", lambda *_: pytest.fail("foreign process signalled"))
    with pytest.raises((ValueError, ProcessLookupError)):
        platform_helpers.signal_observed_group(100, platform.KILL, leader_birth=birth)


def test_recovered_group_signals_only_runtime_bound_members(agent_census_walk, monkeypatch):
    monkeypatch.setattr(platform, "process_group", lambda pid: 10)
    census = {100: SimpleNamespace(birth="root", pgid=100),
              101: SimpleNamespace(birth="child", pgid=100),
              102: SimpleNamespace(birth="escapee", pgid=102)}
    monkeypatch.setattr(process_group, "_process_snapshot", lambda: census)
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"pid": pid, "birth_id": str(pid)})
    sent = []
    monkeypatch.setattr(platform, "signal_process", lambda identity, sig: sent.append((identity, sig)))
    platform_helpers.signal_observed_group(100, platform.KILL, leader_birth="root")
    assert sent == [({"pid": 101, "birth_id": "101"}, platform.KILL),
                    ({"pid": 100, "birth_id": "100"}, platform.KILL)]


@pytest.mark.parametrize("change", ["birth", "group", "missing"])
def test_recovered_group_refuses_member_drift_before_any_signal(agent_census_walk, monkeypatch, change):
    monkeypatch.setattr(platform, "process_group", lambda pid: 10)
    original = {100: SimpleNamespace(birth="root", pgid=100),
                101: SimpleNamespace(birth="child", pgid=100)}
    changed = {100: original[100]}
    if change != "missing":
        changed[101] = SimpleNamespace(
            birth="replacement" if change == "birth" else "child",
            pgid=200 if change == "group" else 100,
        )
    observations = iter([original, original, changed])
    monkeypatch.setattr(process_group, "_process_snapshot", lambda: next(observations))
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"pid": pid, "birth_id": str(pid)})
    monkeypatch.setattr(platform, "signal_process", lambda *_: pytest.fail("changed group signalled"))

    with pytest.raises(ProcessLookupError, match="member changed"):
        platform_helpers.signal_observed_group(100, platform.KILL, leader_birth="root")


@pytest.fixture
def linux_walk(monkeypatch):
    monkeypatch.setattr(platform_helpers.sys, "platform", "linux")
    monkeypatch.setattr(platform, "process_group", lambda pid: 10)
    calls = []
    monkeypatch.setattr(platform, "signal_observed_group",
                        lambda pgid, sig, *, leader=None: calls.append((pgid, sig, leader)))
    return calls


def test_linux_recovery_uses_runtime_walk_with_checked_leader(monkeypatch, linux_walk):
    leader = {"pid": 100, "uid": 1000, "birth_id": "proc-start-ticks:5", "process_group": 100}
    monkeypatch.setattr(platform, "process_identity", lambda pid: dict(leader))
    monkeypatch.setattr(process_group, "process_birth_identity", lambda pid=None: "boot:5")
    platform_helpers.signal_observed_group(100, platform.KILL, leader_birth="boot:5")
    assert linux_walk == [(100, platform.KILL, leader)]


def test_linux_recovery_refuses_a_replaced_leader_before_any_signal(monkeypatch, linux_walk):
    monkeypatch.setattr(platform, "process_identity",
                        lambda pid: {"pid": 100, "birth_id": "proc-start-ticks:9", "process_group": 100})
    monkeypatch.setattr(process_group, "process_birth_identity", lambda pid=None: "boot:9")
    with pytest.raises(ProcessLookupError, match="leader was replaced"):
        platform_helpers.signal_observed_group(100, platform.KILL, leader_birth="boot:5")
    assert linux_walk == []


@pytest.mark.parametrize("leader_state", ["reaped", "left-group"])
def test_linux_recovery_without_its_leader_walks_members_only(monkeypatch, linux_walk, leader_state):
    identity = None if leader_state == "reaped" else {"pid": 100, "birth_id": "proc-start-ticks:5", "process_group": 300}
    monkeypatch.setattr(platform, "process_identity", lambda pid: identity)
    monkeypatch.setattr(process_group, "process_birth_identity", lambda pid=None: "boot:5")
    platform_helpers.signal_observed_group(100, platform.TERM, leader_birth="boot:5")
    assert linux_walk == [(100, platform.TERM, None)]


def test_linux_recovery_of_an_empty_group_stays_quiet(monkeypatch):
    monkeypatch.setattr(platform_helpers.sys, "platform", "linux")
    monkeypatch.setattr(platform, "process_group", lambda pid: 10)
    monkeypatch.setattr(platform, "process_identity", lambda pid: None)

    def nothing(pgid, sig, *, leader=None):
        raise ProcessLookupError(errno.ESRCH, "no process in the group could be signalled")

    monkeypatch.setattr(platform, "signal_observed_group", nothing)
    platform_helpers.signal_observed_group(100, platform.TERM, leader_birth="boot:5")


@pytest.mark.linux_native
def test_linux_recovery_stops_a_real_group_whose_leader_was_reaped(tmp_path):
    code = ("import subprocess,sys,time; "
            "[subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']) for _ in range(2)]; "
            "time.sleep(60)")
    leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    pgid = leader.pid
    birth = process_group.process_birth_identity(pgid)
    import time

    def members():
        return [p for p, info in process_group._process_snapshot().items()
                if info.pgid == pgid and platform.process_identity(p) is not None]  # zombies are gone

    deadline = time.monotonic() + 10
    while len(members()) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    leader.kill()
    leader.wait(timeout=5)  # reaped: the pgid no longer names a live leader
    assert members()
    platform_helpers.signal_observed_group(pgid, platform.KILL, leader_birth=birth)
    deadline = time.monotonic() + 10
    while members() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not members()


WINDOWS_ONLY = pytest.mark.windows_native


@WINDOWS_ONLY
def test_windows_scope_job_kills_descendants_after_leader_exit(tmp_path):
    marker = tmp_path / "child.pid"
    code = ("import subprocess,sys,pathlib; "
            "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(c.pid))")
    process = task_scope.spawn([sys.executable, "-c", code, str(marker)])
    try:
        assert process.wait(timeout=10) == 0
        child = int(marker.read_text())
        assert platform.process_identity(child) is not None
        assert task_scope.stop(process, grace=0.1).status == "settled"
        import time
        deadline = time.monotonic() + 10
        while platform.process_identity(child) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert platform.process_identity(child) is None
    finally:
        platform.stop_tree(process, grace=0.1)


@WINDOWS_ONLY
def test_windows_lock_and_owner_permissions_are_native(tmp_path):
    path = tmp_path / "owner.lock"
    with platform.file_lock(path) as fd:
        assert platform.is_owner_only(fd)
        second = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                platform.lock_file(second, blocking=False)
        finally:
            os.close(second)
    with platform.file_lock(path, blocking=False):
        pass


@WINDOWS_ONLY
def test_windows_scope_dies_when_owner_process_exits(tmp_path):
    marker = tmp_path / "child.pid"
    code = ("import os,sys,pathlib; from astrid.core.execution import task_scope; "
            "c=task_scope.spawn([sys.executable,'-c','import time;time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(c.pid)); os._exit(0)")
    result = subprocess.run([sys.executable, "-c", code, str(marker)], timeout=10)
    assert result.returncode == 0
    child = int(marker.read_text())
    import time
    deadline = time.monotonic() + 10
    while platform.process_identity(child) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert platform.process_identity(child) is None


def test_capability_children_configure_from_the_hosts_support_root(tmp_path, monkeypatch):
    from runtime_protocol import process_control

    process_control.write_setting(tmp_path, "sandbox")
    monkeypatch.setattr(platform, "_process_control", None)
    monkeypatch.setattr(platform_helpers, "_support_root", None)
    monkeypatch.setenv(platform_helpers.PROCESS_CONTROL_SUPPORT_ENV, str(tmp_path))
    platform_helpers._configure_from_environment()
    assert platform.process_control() == "sandbox"
    assert platform_helpers.process_control_support() == str(tmp_path)
