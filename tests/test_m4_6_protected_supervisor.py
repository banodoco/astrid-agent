import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import m4_6_protected_supervisor as supervisor
from scripts.m4_6_process_observer import (
    ManagerIdentity, NativeServiceObservationTarget, ObservationUnavailable, ProcessSnapshot,
)


def target():
    return NativeServiceObservationTarget("linux", "systemd-user", "astrid.service", None,
                                          (Path(sys.executable),))


class SourceFixture:
    """Only native service snapshots are simulated; collector/helper OS reads are real."""

    instances = []
    polls = 0
    fail_at = None
    wrong_target = False

    def __init__(self, bound_target):
        self.target = bound_target
        self.closed = False
        self.instances.append(self)

    def arm_initial_absence(self):
        if self.fail_at == "arm":
            raise ObservationUnavailable("native user manager unavailable")

    def poll(self):
        self.polls += 1
        if self.polls == self.fail_at:
            raise ObservationUnavailable("native checkpoint unavailable")
        return ProcessSnapshot("linux-bounded-proc-systemd-poll",
            ManagerIdentity("systemd-user", "foreign.service" if self.wrong_target
                            else self.target.manager_target, "absent", None, None, None),
            None, (), ())

    def report(self):
        return {"authorization": "refused", "source": "unit-test-service-fixture"}

    def close(self, _reason):
        self.closed = True


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.setattr(supervisor, "ProcessTransitionObserver", SourceFixture)
    instance = supervisor.DiagnosticCheckpointCollector(target())
    instance.arm_initial_absence()
    return instance


def records(collector):
    return [item["record"] for item in json.loads(collector.report_bytes)["records"]]


def test_self_identity_is_os_acquired_with_real_executable_hash():
    observed = supervisor.observe_self()
    assert observed.pid == os.getpid()
    assert observed.uids == (os.getuid(),) * 4
    assert observed.executable == str(Path(sys.executable).resolve())
    assert observed.executable_sha256 == hashlib.sha256(Path("/proc/self/exe").read_bytes()).hexdigest()
    assert observed.cwd == str(Path.cwd())
    assert observed.argv == tuple(os.fsdecode(item) for item in
                                 Path("/proc/self/cmdline").read_bytes()[:-1].split(b"\0"))


def test_owns_actual_child_request_and_two_retained_snapshots(collector):
    encoded = collector.collect_diagnostic()
    result = json.loads(encoded)
    assert result["state"] == "diagnostic-complete"
    assert result["authority"] == "none" and result["admission"] == "refused"
    assert result["inner_uninstall_exit"] is None and result["settlement"] == "unavailable"
    rows = {row["kind"]: row["value"] for row in records(collector)}
    child = rows["child"]
    assert child["pid"] != os.getpid() and child["parent_pid"] == os.getpid()
    assert child["uids"] == [os.getuid()] * 4
    assert child["argv"] == [rows["collector"]["executable"], "-I", "-S", "-c", supervisor._HELPER]
    assert child["cwd"] == rows["collector"]["cwd"]
    assert child["executable_sha256"] == rows["collector"]["executable_sha256"]
    assert rows["child-reaped"]["outer_exit"] == 0
    assert rows["initial-diagnostic-checkpoint"] == rows["final-diagnostic-checkpoint"]
    request = rows["diagnostic-request"]
    assert len(request["request_id"]) == 32
    assert request["sha256"] == hashlib.sha256(request["request_id"].encode()).hexdigest()
    for row in result["records"]:
        assert row["sha256"] == hashlib.sha256(supervisor._encode(row["record"])).hexdigest()
    assert SourceFixture.instances[-1].closed
    with pytest.raises(ProcessLookupError):
        os.kill(child["pid"], 0)
    result["records"].clear()
    assert collector.report_bytes == encoded
    with pytest.raises(supervisor.SupervisorRefused, match="consumed"):
        collector.collect_diagnostic()


def test_public_protected_entry_refuses_without_observation_or_launch(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("protected entry performed an action")
    monkeypatch.setattr(supervisor, "observe_self", forbidden)
    monkeypatch.setattr(supervisor.subprocess, "Popen", forbidden)
    with pytest.raises(supervisor.SupervisorRefused, match="channel unavailable"):
        supervisor.open_protected_supervisor()


def test_no_caller_identity_snapshot_exit_or_settlement_interface(collector):
    with pytest.raises(TypeError):
        collector.collect_diagnostic(identity={}, initial={}, final={}, exit_code=0, settled=True)
    for stage in (False, True):
        if stage:
            collector.collect_diagnostic()
        with pytest.raises(supervisor.SupervisorRefused, match="inner uninstall exit and settlement"):
            collector.finalize_removal()


@pytest.mark.parametrize("fail_at", [1, 2])
def test_failed_observation_retains_abort_and_consumes(monkeypatch, collector, fail_at):
    monkeypatch.setattr(SourceFixture, "fail_at", fail_at)
    with pytest.raises(supervisor.SupervisorRefused, match="retained"):
        collector.collect_diagnostic()
    rows = records(collector)
    assert json.loads(collector.report_bytes)["state"] == "refused"
    assert any(row["kind"] == ("diagnostic-failed" if fail_at == 1 else "final-observation-failed")
               for row in rows)
    if fail_at == 1:
        assert not any(row["kind"] == "launched-pid" for row in rows)
    else:
        assert any(row["kind"] == "child-reaped" for row in rows)
    with pytest.raises(supervisor.SupervisorRefused, match="consumed"):
        collector.collect_diagnostic()


def test_foreign_observer_target_refused_before_helper(monkeypatch, collector):
    monkeypatch.setattr(SourceFixture, "wrong_target", True)
    with pytest.raises(supervisor.SupervisorRefused):
        collector.collect_diagnostic()
    assert not any(row["kind"] == "launched-pid" for row in records(collector))


def test_arm_failure_is_not_retryable(monkeypatch):
    monkeypatch.setattr(supervisor, "ProcessTransitionObserver", SourceFixture)
    monkeypatch.setattr(SourceFixture, "fail_at", "arm")
    collector = supervisor.DiagnosticCheckpointCollector(target())
    with pytest.raises(supervisor.SupervisorRefused, match="initial absence"):
        collector.arm_initial_absence()
    with pytest.raises(supervisor.SupervisorRefused, match="already attempted"):
        collector.arm_initial_absence()
    assert SourceFixture.instances[-1].closed


@pytest.mark.parametrize("suffix", [
    "print('{}', flush=True)",
    "print('{\"diagnostic_request_sha256\":\"foreign\"}', flush=True)",
    "print('{\"exit_code\":false,\"settled\":true}', flush=True)",
    "raise SystemExit(73)",
    "import signal, os; os.kill(os.getpid(), signal.SIGTERM)",
    "import time; time.sleep(20)",
    "print('x' * 70000, flush=True)",
])
def test_wrong_reply_nonzero_signal_timeout_and_overflow_refuse(monkeypatch, collector, suffix):
    # The injected source is adversarial test material, never configurable in the API.
    monkeypatch.setattr(supervisor, "_HELPER",
                        "import sys\nprint('ready', flush=True)\nsys.stdin.buffer.readline()\n" + suffix)
    with pytest.raises(supervisor.SupervisorRefused, match="retained"):
        collector.collect_diagnostic()
    rows = {row["kind"]: row["value"] for row in records(collector)}
    assert rows["child-reaped"]["outer_exit"] is not None
    assert "final-diagnostic-checkpoint" in rows
    with pytest.raises(supervisor.SupervisorRefused, match="consumed"):
        collector.collect_diagnostic()
    with pytest.raises(supervisor.SupervisorRefused):
        collector.finalize_removal()


def test_partial_readiness_has_a_deadline(monkeypatch, collector):
    monkeypatch.setattr(supervisor, "_HELPER",
                        "import os,time; os.write(1,b'r'); time.sleep(20)")
    with pytest.raises(supervisor.SupervisorRefused, match="retained"):
        collector.collect_diagnostic()
    assert any(row["kind"] == "child-reaped" for row in records(collector))


@pytest.mark.parametrize("field,value", [
    ("birth", "different"), ("uids", (12345,) * 4), ("parent_pid", 12345),
    ("executable_sha256", "f" * 64), ("argv", ("foreign",)), ("cwd", "/foreign"),
])
def test_actual_child_mismatch_is_retained_and_reaped(monkeypatch, collector, field, value):
    original = supervisor._PinnedProcess.capture
    def altered(pin):
        observed = original(pin)
        if observed.pid != os.getpid():
            if field == "birth":
                raise supervisor.SupervisorRefused("process identity changed during acquisition")
            return replace(observed, **{field: value})
        return observed
    monkeypatch.setattr(supervisor._PinnedProcess, "capture", altered)
    with pytest.raises(supervisor.SupervisorRefused, match="retained"):
        collector.collect_diagnostic()
    assert any(row["kind"] == "child-reaped" for row in records(collector))


@pytest.mark.parametrize("bad", [True, 0, -1, "1"])
def test_pid_only_or_malformed_handle_refused(bad):
    with pytest.raises(supervisor.SupervisorRefused):
        supervisor._PinnedProcess(bad)


def test_changed_identity_between_os_reads_refuses(monkeypatch):
    original = supervisor._read_process
    count = 0
    def changing(pid):
        nonlocal count
        count += 1
        observed = original(pid)
        return observed if count == 1 else replace(observed, birth=observed.birth + "1")
    monkeypatch.setattr(supervisor, "_read_process", changing)
    with pytest.raises(supervisor.SupervisorRefused, match="changed"):
        supervisor.observe_self()


@pytest.mark.parametrize("malformation", ["short-stat", "bad-birth", "missing-uids",
                                         "duplicate-uids", "mixed-uids", "bad-argv"])
def test_malformed_os_identity_refuses(monkeypatch, malformation):
    original_text, original_bytes = Path.read_text, Path.read_bytes
    def text(path, *args, **kwargs):
        value = original_text(path, *args, **kwargs)
        if str(path) == f"/proc/{os.getpid()}/stat":
            if malformation == "short-stat":
                return f"{os.getpid()} (comm) R 1"
            if malformation == "bad-birth":
                end = value.rfind(") ") + 2
                fields = value[end:].split()
                fields[19] = "unknown"
                return value[:end] + " ".join(fields)
        if str(path) == f"/proc/{os.getpid()}/status":
            if malformation == "missing-uids":
                return "Name:\ttest\n"
            if malformation == "duplicate-uids":
                return value + "\nUid:\t0 0 0 0\n"
            if malformation == "mixed-uids":
                return "Uid:\t0 1 0 0\n"
        return value
    def binary(path, *args, **kwargs):
        if str(path) == f"/proc/{os.getpid()}/cmdline" and malformation == "bad-argv":
            return b"missing-null"
        return original_bytes(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", text)
    monkeypatch.setattr(Path, "read_bytes", binary)
    with pytest.raises(supervisor.SupervisorRefused, match="malformed|mixed"):
        supervisor.observe_self()
