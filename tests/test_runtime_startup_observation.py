"""Focused RuntimeCLI transport tests with a simulated native startup clock."""

from __future__ import annotations

import json
import subprocess

import pytest

from astrid import runtime_cli
from astrid.sdk.storage_root import DATA_ROOT_ENV


OPERATION = {
    "operation_id": "a" * 32,
    "state": "pending",
    "phase": "checking_history",
    "objects_checked": 24,
}
LAUNCHER = {"state": "starting_service", "attempt_id": "b" * 32}
UNAVAILABLE = (1, {"state": "unavailable"}, "")
REFUSED = (1, {"ok": False, "error": "Runtime owner identity mismatch"}, "")


class RuntimeProcess:
    def __init__(self, argv, call, response, runtime):
        self.args = argv
        self.call = call
        self.response = response
        self.runtime = runtime
        self.returncode = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.wait()

    def communicate(self, input=None, timeout=None):
        self.runtime.waits.append((self.call, timeout))
        if self.call == self.runtime.slow_call:
            self.runtime.crossed_deadline.append(self.call)
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode, data, stderr = self.response
        return json.dumps(data) if data is not None else "", stderr

    def kill(self):
        self.runtime.signals.append(self.call)
        self.returncode = -9

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode


class NativeRuntime:
    def __init__(self, support, realm):
        self.support, self.realm = support, realm
        self.status_replies = []
        self.slow_call = None
        self.inspect_replies = {}
        self.up_reply = (0, {"ok": True, "realm_id": "realm-a"}, "")
        self.commands = []
        self.waits = []
        self.signals = []
        self.crossed_deadline = []
        self.inspections = 0

    def popen(self, argv, **kwargs):
        command = argv[1:]
        self.commands.append(command)
        assert command[command.index("--data-root") + 1] == str(self.support)
        assert "--json" in command
        if command[0] == "startup-status":
            call = "status"
            response = self.status_replies.pop(0) if self.status_replies else UNAVAILABLE
        elif command[:2] == ["workspace", "inspect"]:
            self.inspections += 1
            call = f"inspect-{self.inspections}"
            response = self.inspect_replies.get(self.inspections, (
                0, {"ok": True, "state": "configured", "realm_id": "realm-a",
                    "realm_root": str(self.realm), "support_root": str(self.support)}, "",
            ))
        else:
            assert command[0] == "up", command
            call = "up"
            response = self.up_reply
        return RuntimeProcess(argv, call, response, self)


@pytest.fixture
def native(tmp_path, monkeypatch):
    runtime = NativeRuntime(tmp_path / "support", tmp_path / "realm")
    monkeypatch.setenv(DATA_ROOT_ENV, str(runtime.support))
    monkeypatch.setattr(runtime_cli, "_runtime_command", lambda: ("native-runtime",))
    monkeypatch.setattr(subprocess, "Popen", runtime.popen)
    return runtime


def public_up(native, capsys):
    code = runtime_cli.main(["up", "--data-root", str(native.support), "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("slow_call", ["inspect-1", "inspect-2", "up"])
def test_up_keeps_the_exact_child_attached_across_the_old_deadline(native, capsys, slow_call):
    native.slow_call = slow_call

    code, data = public_up(native, capsys)

    assert native.crossed_deadline == [slow_call]
    assert native.signals == []
    assert (code, data) == (0, {"ok": True, "realm_id": "realm-a"})
    assert [(call, timeout) for call, timeout in native.waits if call != "status"] == [
        ("inspect-1", None), ("inspect-2", None), ("up", None),
    ]
    assert all("--startup-operation" in command for command in native.commands if command[0] == "workspace")


@pytest.mark.parametrize(
    "completed_inspections, observation, key",
    [
        (0, OPERATION, "startup_operation"),
        (1, LAUNCHER, "startup_attempt"),
        (1, {**LAUNCHER, "state": "inspecting_workspace"}, "startup_attempt"),
        (2, LAUNCHER, "startup_attempt"),
    ],
)
def test_up_observes_a_live_owner_without_another_inspection_or_launch(
    native, capsys, completed_inspections, observation, key,
):
    native.status_replies = [UNAVAILABLE] * completed_inspections + [(0, observation, "")]

    code, data = public_up(native, capsys)

    assert native.inspections == completed_inspections
    assert not any(command[0] == "up" for command in native.commands)
    assert native.signals == []
    assert code == 1
    assert data["ok"] is False
    assert data["problem_code"] == "startup_pending"
    assert data[key] == observation
    if completed_inspections:
        assert "--expected-realm-id" in native.commands[-1]
        assert native.commands[-1][native.commands[-1].index("--realm-root") + 1] == str(native.realm)


def test_guarded_inspection_preserves_pending_when_the_owner_appears_after_status(native, capsys):
    native.inspect_replies[2] = (
        1, {"ok": False, "state": "startup_pending", "startup_operation": OPERATION}, "",
    )
    native.status_replies = [UNAVAILABLE, UNAVAILABLE, (0, OPERATION, "")]

    code, data = public_up(native, capsys)

    assert code == 1
    assert data["problem_code"] == "startup_pending"
    assert data["startup_operation"] == OPERATION
    assert not any(command[0] == "up" for command in native.commands)
    assert native.signals == []


@pytest.mark.parametrize("status", [UNAVAILABLE, (0, {**OPERATION, "state": "ended"}, ""),
                                    (0, {**LAUNCHER, "state": "failed"}, "")])
def test_observed_absence_or_end_preserves_native_terminal_refusal(native, capsys, status):
    native.status_replies = [status] * 3
    native.up_reply = REFUSED

    assert public_up(native, capsys) == REFUSED[:2]
    assert [call for call, _ in native.waits if call != "status"] == ["inspect-1", "inspect-2", "up"]
    assert native.signals == []


def test_old_runtime_keeps_the_original_commands_and_deadlines(native, capsys):
    unsupported = "astrid-runtime: error: argument command: invalid choice: 'startup-status' (choose from 'up', 'workspace')"
    native.status_replies = [(2, None, unsupported)] * 3

    assert public_up(native, capsys) == (0, {"ok": True, "realm_id": "realm-a"})
    assert [(call, timeout) for call, timeout in native.waits if call != "status"] == [
        ("inspect-1", 120.0), ("inspect-2", 120.0), ("up", 120.0),
    ]
    assert not any("--startup-operation" in command for command in native.commands)


def test_runtime_without_installed_startup_scope_keeps_the_legacy_deadlines(native, capsys):
    out_of_scope = (
        1, {"ok": False, "error": "Runtime startup operations require the selected installed Runtime environment."}, "",
    )
    native.inspect_replies = {1: out_of_scope, 3: out_of_scope}

    assert public_up(native, capsys) == (0, {"ok": True, "realm_id": "realm-a"})
    assert [(call, timeout) for call, timeout in native.waits if call != "status"] == [
        ("inspect-1", None), ("inspect-2", 120.0), ("inspect-3", None), ("inspect-4", 120.0), ("up", 120.0),
    ]


def test_invalid_startup_status_does_not_authorize_an_inspection_or_launch(native, capsys):
    native.status_replies = [(0, {**LAUNCHER, "attempt_id": "unverified"}, "")]

    code, data = public_up(native, capsys)

    assert code == 1
    assert data["problem_code"] == "runtime_unavailable"
    assert data["error"] == "Runtime startup status could not be verified"
    assert [call for call, _ in native.waits] == ["status"]


def test_up_preserves_both_workspace_identity_checks(native):
    runtime = runtime_cli.RuntimeCLI()
    preflight = dict(support_root=native.support, expected_realm_id="realm-a", realm_root=native.realm)
    native.inspect_replies[1] = (0, {
        "ok": True, "realm_id": "realm-b", "realm_root": str(native.realm),
    }, "")
    with pytest.raises(runtime_cli.RuntimeCLIError, match="Runtime selected workspace 'realm-b'"):
        runtime.up(**preflight)
    assert not any(command[0] == "up" for command in native.commands)

    native.up_reply = (0, {"ok": True, "realm_id": "realm-b"}, "")
    with pytest.raises(runtime_cli.RuntimeCLIError, match="Runtime selected workspace 'realm-b'"):
        runtime.up(**preflight)
    assert sum(command[0] == "up" for command in native.commands) == 1
