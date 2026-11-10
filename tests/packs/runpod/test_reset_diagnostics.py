"""Reset failures cross the pod/controller boundary as fixed reasons only."""
import contextlib
import json
from types import SimpleNamespace

import pytest

from astrid.core import machines_cli
from astrid.packs.runpod import machine, pod_reset, remote_placement

SECRET = "provider-token-must-not-escape"


@pytest.mark.parametrize("stage", sorted(pod_reset.RESET_FAILURE_REASONS - {"unknown"}))
def test_reset_cli_reports_only_fixed_substep_reason(tmp_path, monkeypatch, capsys, stage):
    original = pod_reset.reset
    calls = []

    def step(name, value):
        if stage == name:
            raise RuntimeError(SECRET)
        return value

    @contextlib.contextmanager
    def lock(path):
        step("prepare", None)
        yield

    def ensure():
        name = "ready_ensure" if calls else "runtime_ensure"
        calls.append(name)
        return step(name, {"realm_id": "realm", "endpoint": "loopback"})

    monkeypatch.setattr(pod_reset, "reset_lock", lock)
    monkeypatch.setattr(pod_reset, "realm_root", lambda *a: step("realm_lookup", tmp_path))
    monkeypatch.setattr(pod_reset, "_owner_token", lambda *a: "unused")
    monkeypatch.setattr(pod_reset, "take_template", lambda *a, **k: step("template_create", {}))
    monkeypatch.setattr(pod_reset, "replace_realm", lambda *a, **k: step("template_restore", {}))
    monkeypatch.setattr(pod_reset, "sweep", lambda **k: step("sweep", []))
    monkeypatch.setattr(pod_reset, "write_prepared", lambda *a, **k: step("ready_write", {}))
    template = tmp_path / "template"
    if stage == "template_restore":
        template.mkdir()
    monkeypatch.setattr(pod_reset, "reset", lambda **k: original(
        ensure=ensure, data_root=tmp_path, template=template, ready=tmp_path / "ready",
        prepared=tmp_path / "prepared", active=tmp_path / "active", lock=tmp_path / "lock"))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(tmp_path / "sources.json"))
    monkeypatch.setenv("ASTRID_RUNTIME_DOOR_PORT", "0")
    assert pod_reset.main(["reset"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"reset": "failed", "reason": stage}
    assert SECRET not in output.out + output.err and output.err == ""


@pytest.mark.parametrize("stdout,reason", [
    ('{"reset":"failed","reason":"realm_lookup"}', "realm_lookup"),
    ('{"reset":"failed","reason":"' + SECRET + '"}', "unknown"),
    ('{"reset":"failed","reason":{"token":"' + SECRET + '"}}', "unknown"),
    ('{"reset":"failed","error":"' + SECRET + '"}', "unknown"),
    ('{"reset":"failed","reason":"sweep","error":"' + SECRET + '"}', "unknown"),
    ('["' + SECRET + '"]', "unknown"),
    (SECRET, "unknown"), ("", "unknown"),
])
def test_controller_reset_failure_never_copies_stdout_or_stderr(tmp_path, monkeypatch, stdout, reason):
    class Ops:
        def job_start(self, command):
            return {"id": "a" * 32, "dir": "/jobs/job-exact"}

        def job_poll(self, job):
            return {"state": "exited", "exit_code": 1}

        def exec(self, command, timeout):
            return 0, stdout if command.startswith("cat ") else SECRET, ""

    placement = remote_placement.RemotePlacement(None)
    monkeypatch.setattr(placement, "_check_deadline", lambda *args: None)
    state = remote_placement.RemoteRun("run", "/run", tmp_path, claim=SimpleNamespace(ops=Ops()))
    with pytest.raises(remote_placement.RemotePlacementError) as caught:
        placement._run_job(state, "reset-command", "pod reset")
    assert str(caught.value) == f"pod reset job {'a' * 32} exited 1: reset reason: {reason}"
    assert SECRET not in str(caught.value)


def test_zero_exit_failed_reset_cannot_leak_old_error_field(tmp_path):
    placement = SimpleNamespace(
        _exec=lambda *a, **k: (0, "pod-state:absent", ""),
        _run_job=lambda *a: json.dumps({"reset": "failed", "error": SECRET}),
    )
    with pytest.raises(machine.MachineError, match="pod reset failed: unknown") as caught:
        machine.ensure_pod_ready(placement, SimpleNamespace())
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("as_json", [False, True])
def test_release_of_already_ended_machine_is_idempotent_without_touching_replacement(monkeypatch, capsys, as_json):
    ended = {"id": "old-pod", "state": "ended", "live": False, "ended": "preparation_failed"}
    live = {"id": "new-pod", "state": "active", "live": True}
    monkeypatch.setattr(machines_cli, "_leases", lambda: [live, ended])

    def forbidden():
        pytest.fail("an ended machine cannot dispatch release of its replacement")

    argv = ["release", "old-pod"] + (["--json"] if as_json else [])
    assert machines_cli.main(argv, release=forbidden) == 0
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output) == {"ok": True, "machine": ended}
    else:
        assert output.strip() == "already released old-pod"
    assert machines_cli.main(["release", "unknown", "--json"], release=forbidden) == 2


@pytest.mark.parametrize("exit_code", [SECRET, {"secret": SECRET}, False])
def test_hostile_exit_code_and_job_identity_are_not_reset_diagnostics(tmp_path, monkeypatch, exit_code):
    ops = SimpleNamespace(
        job_start=lambda command: {"id": SECRET, "dir": "/jobs/exact"},
        job_poll=lambda job: {"state": "exited", "exit_code": exit_code},
        exec=lambda *a: (0, '{"reset":"failed","reason":"sweep"}', SECRET),
    )
    placement = remote_placement.RemotePlacement(None)
    monkeypatch.setattr(placement, "_check_deadline", lambda *args: None)
    state = remote_placement.RemoteRun("run", "/run", tmp_path, claim=SimpleNamespace(ops=ops))
    with pytest.raises(remote_placement.RemotePlacementError) as caught:
        placement._run_job(state, "reset-command", "pod reset")
    assert str(caught.value) == "pod reset job unknown exited unknown: reset reason: sweep"
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("exit_code,result,reason", [
    (SECRET, {"reset": "ok"}, "unknown"),
    ({"secret": SECRET}, {"reset": "failed", "reason": "sweep"}, "sweep"),
    (1, {"reset": "failed", "error": SECRET}, "unknown"),
])
def test_post_job_reset_also_rejects_hostile_diagnostics(monkeypatch, exit_code, result, reason):
    ops = SimpleNamespace(
        job_start=lambda command, job_id: {"id": job_id, "dir": "/jobs/exact"},
        job_poll=lambda job: {"state": "exited", "exit_code": exit_code},
        exec=lambda *a: (0, json.dumps(result), SECRET),
    )
    runner = machine.RunPodMachine()
    monkeypatch.setattr(runner, "_checkpoint", lambda *args: None)
    lease = SimpleNamespace(claim=SimpleNamespace(ops=ops), run_dir="/runs/exact", job_dir="/jobs/previous")
    with pytest.raises(machine.MachineError) as caught:
        runner._reset(lease)
    safe_code = "1" if exit_code == 1 else "unknown"
    assert str(caught.value) == f"pod reset failed (exit {safe_code}): {reason}"
    assert SECRET not in str(caught.value)


def test_reset_cli_hides_unclassified_raw_exception(tmp_path, monkeypatch, capsys):
    def fail(**kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(pod_reset, "reset", fail)
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(tmp_path / "sources.json"))
    monkeypatch.setenv("ASTRID_RUNTIME_DOOR_PORT", "0")
    assert pod_reset.main(["reset"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"reset": "failed", "reason": "unknown"}
    assert SECRET not in output.out + output.err and output.err == ""
