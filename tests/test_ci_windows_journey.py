"""Guard against a journey going green on admission, stale readiness or PID reuse."""

import json
from pathlib import Path, PureWindowsPath
import subprocess
import sys

import pytest

from scripts import ci_windows_journey as journey
from scripts import ci_journey as shared_journey


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-journey.yml"


def test_tst_b1_windows_alias_requires_owned_roots_before_install(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ci_journey", shared_journey)
    monkeypatch.setattr(sys, "argv", ["ci_windows_journey.py", "journey", "--leg", "fresh-install",
                                   "--app-checkout", str(tmp_path)])
    monkeypatch.setattr(shared_journey, "bootstrap", lambda *unused: (_ for _ in ()).throw(
        AssertionError("bootstrap must not run")))
    with pytest.raises(SystemExit) as failure:
        journey.main()
    assert failure.value.code == 2


def test_tst_b1_windows_alias_returns_shared_driver_failure(monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ci_journey", shared_journey)
    monkeypatch.setattr(sys, "argv", ["ci_windows_journey.py", "journey"])
    monkeypatch.setattr(shared_journey, "main", lambda unused: 1)
    assert journey.main() == 1


def test_m4_4a_backend_uses_windows_powershell_51_admission_route():
    command = journey.powershell5_command(r"C:\Program Files\nodejs\node.exe",
                                          ["-e", "process.exit(0)", r"C:\Pinokio\api\Astrid"])
    assert command[:5] == ["powershell.exe", "-NoProfile", "-NonInteractive",
                           "-ExecutionPolicy", "Bypass"]
    assert "& 'C:\\Program Files\\nodejs\\node.exe'" in command[-1]
    assert "process.exit(0)" in command[-1]
    assert "pwsh" not in command[0].lower()


@pytest.mark.parametrize("action", sorted(journey.OFFLINE_WORKSPACE_ACTIONS))
def test_offline_workspace_stop_disables_repeating_task_before_stop(monkeypatch, action):
    calls = []
    monkeypatch.setattr(journey, "run", lambda argv, name, **kwargs: calls.append(
        (argv, name, kwargs)) or "")

    journey.powershell(journey.OFFLINE_WORKSPACE_STOP, f"{action}-stop-service.log",
                       show=False, timeout=30)

    argv, name, kwargs = calls.pop()
    script = argv[-1]
    assert name == f"{action}-stop-service.log"
    assert kwargs == {"show": False, "timeout": 30}
    assert script.count("Disable-ScheduledTask") == 1
    assert script.count("Stop-ScheduledTask") == 1
    assert script.index("Disable-ScheduledTask") < script.index("Stop-ScheduledTask")
    assert script.count("-TaskName 'Astrid Runtime' -TaskPath '\\'") == 2


@pytest.mark.parametrize("action", sorted(journey.OFFLINE_WORKSPACE_ACTIONS))
def test_offline_workspace_start_enables_task_before_restart(monkeypatch, action):
    calls = []
    monkeypatch.setattr(journey, "run", lambda argv, name, **kwargs: calls.append(
        (argv, name, kwargs)) or "")

    journey.powershell(journey.OFFLINE_WORKSPACE_START, f"{action}-start-service.log",
                       show=False, timeout=30)

    argv, name, kwargs = calls.pop()
    script = argv[-1]
    assert name == f"{action}-start-service.log"
    assert kwargs == {"show": False, "timeout": 30}
    assert script.count("Enable-ScheduledTask") == 1
    assert script.count("Start-ScheduledTask") == 1
    assert script.index("Enable-ScheduledTask") < script.index("Start-ScheduledTask")
    assert script.count("-TaskName 'Astrid Runtime' -TaskPath '\\'") == 2


def test_offline_workspace_task_command_rejects_custody_drift(monkeypatch):
    monkeypatch.setattr(journey, "run", lambda *args, **kwargs: pytest.fail(
        "a changed protected task command must not execute"))
    with pytest.raises(RuntimeError, match="differs from the protected task action"):
        journey.powershell("Stop-ScheduledTask -TaskName 'Other'", "seed-stop-service.log")


def test_non_workspace_task_command_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(journey, "run", lambda argv, name, **kwargs: calls.append(argv) or "")
    code = "Start-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'"
    journey.powershell(code, "start.log")
    assert calls[0][-1].endswith(code)
    assert "Enable-ScheduledTask" not in calls[0][-1]


@pytest.mark.parametrize("helper_fails", [False, True], ids=["success", "helper-failure"])
def test_release0_offline_workspace_blocks_repetition_reacquisition(
        tmp_path, monkeypatch, helper_fails):
    from scripts import journey_version_jumps as jumps

    state = {"enabled": True, "running": True, "workspace_opened": False}
    calls = []
    boundaries = []
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence" / "release0-api-seed")
    monkeypatch.setattr(journey, "_capture_release0_seed_boundary", boundaries.append)

    def ownership(*unused):
        assert state["running"]
        return {"manager": "task-scheduler"}

    monkeypatch.setattr(shared_journey, "assert_direct_service_ownership", ownership)
    monkeypatch.setattr(shared_journey, "assert_one_runtime",
                        lambda *unused: state["running"] or pytest.fail("owner is absent"))
    monkeypatch.setattr(shared_journey, "workspace_root", lambda unused: tmp_path / "realm")
    monkeypatch.setattr(shared_journey, "runtime_processes",
                        lambda unused: [41] if state["running"] else [])
    monkeypatch.setattr(shared_journey, "wait_ready",
                        lambda unused: state["running"] or pytest.fail("owner was not restored"))

    def run(argv, name, **unused):
        script = argv[-1]
        calls.append(name)
        if name == "seed-stop-service.log":
            assert script.index("Disable-ScheduledTask") < script.index("Stop-ScheduledTask")
            state.update(enabled=False, running=False)
            # Model the one-minute trigger that landed inside the failing run's
            # stop-to-seed window. A still-enabled task would reacquire here.
            if state["enabled"]:
                state["running"] = True
        elif name == "seed-start-service.log":
            assert script.index("Enable-ScheduledTask") < script.index("Start-ScheduledTask")
            state.update(enabled=True, running=True)
        else:
            pytest.fail(f"unexpected manager command {name}")
        return ""

    monkeypatch.setattr(journey, "run", run)

    def workspace(*unused, **kwargs):
        assert not state["running"]
        assert kwargs["realm"] == tmp_path / "realm"
        state["workspace_opened"] = True
        if helper_fails:
            raise ValueError("simulated helper failure")
        return {"seeded": True}

    monkeypatch.setattr(jumps, "workspace", workspace)
    def call():
        return jumps.direct_workspace(
            tmp_path / "program", {}, tmp_path / "data",
            tmp_path / "evidence" / "release0-api-seed", "seed", {})
    if helper_fails:
        with pytest.raises(ValueError, match="simulated helper failure"):
            call()
    else:
        assert call() == {"seeded": True}
    assert calls == ["seed-stop-service.log", "seed-start-service.log"]
    # The after boundary is reached from direct_workspace's finally block, so
    # it is retained even when the offline seed helper raises.
    assert boundaries == ["before-seed", "after-seed"]
    assert state == {"enabled": True, "running": True, "workspace_opened": True}


def test_release0_seed_boundary_capture_records_task_and_run_owned_owners(tmp_path, monkeypatch):
    evidence = tmp_path / "run" / "evidence" / "release0-to-release1" / "release0-api-seed"
    events = []
    observations = [
        {
            "schema_version": 1,
            "receipt_kind": "windows-release0-seed-boundary",
            "task": {"status": "present", "enabled": {"status": "observed", "value": False},
                     "running": {"status": "observed", "value": False}},
            "runtime_owner_census": {"status": "observed", "owner_record_limit": 8,
                                      "owners": [{"pid": 71, "parent_pid": 7,
                                                  "creation_time": {"status": "observed", "value": "before"},
                                                  "command_identity": {"status": "observed", "sha256": "a" * 64}}]},
        },
        {
            "schema_version": 1,
            "receipt_kind": "windows-release0-seed-boundary",
            "task": {"status": "present", "enabled": {"status": "observed", "value": False},
                     "running": {"status": "observed", "value": True}},
            "runtime_owner_census": {"status": "observed", "owner_record_limit": 8,
                                      "owners": [{"pid": 72, "parent_pid": 71,
                                                  "creation_time": {"status": "observed", "value": "after"},
                                                  "command_identity": {"status": "observed", "sha256": "b" * 64}}]},
        },
    ]
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "EVIDENCE", evidence)
    monkeypatch.setenv("ASTRID_CI_PROGRAM_ROOT", r"C:\\journey\\program")
    monkeypatch.setenv("ASTRID_CI_DATA_ROOT", r"C:\\journey\\data")
    monkeypatch.setattr(journey, "run", lambda _argv, name, **_kwargs: events.append(name) or "")

    def capture(argv, **kwargs):
        events.append("capture")
        assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        assert "Get-ScheduledTask -TaskName 'Astrid Runtime'" in argv[-1]
        assert "Get-ScheduledTaskInfo" in argv[-1] and "Get-CimInstance Win32_Process" in argv[-1]
        assert "Stop-ScheduledTask" not in argv[-1] and "Start-ScheduledTask" not in argv[-1]
        assert "PSModulePath" not in {key for key in kwargs["env"] if key.casefold() == "psmodulepath"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(observations.pop(0)).encode(), b"")

    monkeypatch.setattr(journey.subprocess, "run", capture)
    journey.powershell(journey.OFFLINE_WORKSPACE_STOP, "seed-stop-service.log", show=False, timeout=30)
    # Model the failing seed path: direct_workspace still invokes the protected
    # restart command from finally, which captures the failure boundary first.
    journey.powershell(journey.OFFLINE_WORKSPACE_START, "seed-start-service.log", show=False, timeout=30)

    assert events == ["seed-stop-service.log", "capture", "capture", "seed-start-service.log"]
    before = json.loads((evidence.parent / "runtime-service-service-seed-boundary-before-seed.log").read_text())
    after = json.loads((evidence.parent / "runtime-service-service-seed-boundary-after-seed.log").read_text())
    assert before["capture"] == {"status": "captured", "exit_code": 0}
    assert before["task"]["enabled"]["value"] is False
    assert before["runtime_owner_census"]["owners"][0]["pid"] == 71
    assert after["task"]["running"]["value"] is True
    assert after["runtime_owner_census"]["owners"][0]["command_identity"]["sha256"] == "b" * 64


def test_release0_seed_boundary_capture_failure_does_not_change_seed_commands(tmp_path, monkeypatch):
    evidence = tmp_path / "run" / "evidence" / "release0-to-release1" / "release0-api-seed"
    commands = []
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "EVIDENCE", evidence)
    monkeypatch.setattr(journey, "program_root", lambda: tmp_path / "program")
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path / "data")
    monkeypatch.setattr(journey, "run", lambda _argv, name, **_kwargs: commands.append(name) or "")
    monkeypatch.setattr(journey.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(args[0], 30)))

    journey.powershell(journey.OFFLINE_WORKSPACE_STOP, "seed-stop-service.log", show=False, timeout=30)
    journey.powershell(journey.OFFLINE_WORKSPACE_START, "seed-start-service.log", show=False, timeout=30)

    assert commands == ["seed-stop-service.log", "seed-start-service.log"]
    for boundary in ("before-seed", "after-seed"):
        record = json.loads((evidence.parent / f"runtime-service-service-seed-boundary-{boundary}.log").read_text())
        assert record["capture"]["status"] == "capture-failed"
        assert "TimeoutExpired" in record["capture"]["reason"]


@pytest.fixture
def stub_installed_profile_binding(monkeypatch):
    monkeypatch.setattr(journey, "assert_installed_profile_binding", lambda: None)


def sdk_invocation(argv, task_id="task-1"):
    request = json.loads(argv[0])
    assert journey.os.environ["ASTRID_HOST_EXECUTION_PROFILE"] == "installed"
    assert request == {
        "capability_id": "chiptune.sfx",
        "kind": "executor",
        "project": "windows-journey",
        "inputs": {"kind": "blip", "variant": 1, "pitch": 0, "duration_s": 0.1},
    }
    return json.dumps({
        "content": [{"type": "text", "text": "{}"}],
        "details": {"astridTaskRefs": [{"task_id": task_id}]},
    })


def test_journey_runs_for_main_release_windows_branches_and_main_pull_requests():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'branches: [main, release-1, "windows/**"]' in workflow
    assert "pull_request:\n    branches: [main]" in workflow
    assert "name: Windows task host journey (${{ matrix.installer-shell }})" in workflow
    assert "exploratory" not in workflow


def test_journey_runs_for_main_windows_branches_and_main_pull_requests():
    # Retain Timeline's harness guard with the approved Release-1 branch added.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'branches: [main, release-1, "windows/**"]' in workflow
    assert "pull_request:\n    branches: [main]" in workflow
    assert "name: Windows task host journey (${{ matrix.installer-shell }})" in workflow
    assert "exploratory" not in workflow


def test_journey_resolves_companion_refs_from_durable_defaults():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    lock = json.loads((WORKFLOW.parents[2] / "runtime.lock.json").read_text(encoding="utf-8"))
    assert len(lock["source_commit"]) == 40
    assert "$runtimeRef = $lock.source_commit" in workflow
    assert "ref: ${{ steps.runtime-ref.outputs.ref }}" in workflow
    assert "runtime_ref:" not in workflow
    assert "ref: ${{ steps.app-ref.outputs.ref }}" in workflow
    assert '{ "main" }' in workflow
    assert "^[0-9a-fA-F]{40}$" in workflow
    assert "git -C journey-app rev-parse HEAD" in workflow
    assert "default: windows/task-host" not in workflow
    assert "Run an installed-profile SDK invocation through settlement" in workflow


def test_manifest_replaces_released_sources_and_keeps_app_tools(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "public").mkdir(parents=True)
    (app / "public" / "install-manifest.json").write_text(json.dumps({
        "schema_version": 1, "agent": {"sha": "released-agent"},
        "runtime": {"sha": "released-runtime", "schema_digest": "released-schema"},
        "tools": {"python": {"version": "3.11.13"}},
    }), encoding="utf-8")
    monkeypatch.setattr(journey, "source", lambda repo: {"sha": repo.name, "repo": str(repo)})
    manifest = journey.install_manifest(tmp_path / "agent", tmp_path / "runtime", app,
                                        source_override=True)
    assert manifest["agent"] == {"sha": "agent", "repo": str(tmp_path / "agent")}
    assert manifest["runtime"] == {"sha": "runtime", "repo": str(tmp_path / "runtime")}
    assert manifest["tools"] == {"python": {"version": "3.11.13"}}


def process(pid, ppid, created, exe="", command=""):
    return {"pid": pid, "ppid": ppid, "created": created, "exe": exe, "command": command}


def test_processes_include_generic_descendants_and_surviving_orphans():
    root = PureWindowsPath(r"C:\Users\runner\AppData\Local\Astrid")
    host = process(10, 1, "2026-10-26T10:00:00Z", exe=str(root / "py" / "python.exe").upper())
    child = process(11, 10, "2026-10-26T10:00:01Z", exe=r"C:\uv\python.exe")
    grandchild = process(12, 11, "2026-10-26T10:00:02Z", exe=r"C:\cache\chrome.exe")
    # Child-first ordering, plus a reused parent PID that belongs to an older child.
    unrelated = process(13, 10, "2026-10-26T09:00:00Z", exe=r"C:\other.exe")
    selected = journey.astrid_processes([grandchild, unrelated, child, host], [root])
    assert {row["pid"] for row in selected} == {10, 11, 12}
    assert journey.astrid_processes([grandchild], [root], {journey.process_key(grandchild)}) == [grandchild]
    reused = {**grandchild, "created": "2026-10-26T11:00:00Z"}
    assert journey.astrid_processes([reused], [root], {journey.process_key(grandchild)}) == []


@pytest.mark.parametrize("command", [
    r"C:\Temp\astrid-install-123\venv\Scripts\python.exe -m astrid install",
    r"C:\uv\python.exe -I -m astrid install",
])
def test_partial_install_and_unmarked_job_child_are_not_missed(command):
    installer = process(20, 1, "10:00", command=command)
    child = process(21, 20, "10:01", exe=r"C:\cache\python.exe")
    assert journey.astrid_processes([child, installer], []) == [child, installer]


@pytest.mark.parametrize("state,instance,expected", [
    ("starting", "current", False), ("running", "stale", False), ("running", "current", True),
])
def test_ready_file_must_belong_to_running_service(tmp_path, monkeypatch, state, instance, expected):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "generic-host.ready.json").write_text(json.dumps({
        "status": "ready", "runtime_instance_id": instance,
    }), encoding="utf-8")
    monkeypatch.setattr(journey, "health", lambda: {
        "status": "ok", "runtime_instance_id": "current", "services": [{"name": "pack-host", "state": state}],
    })
    assert journey.host_ready() is expected


@pytest.mark.parametrize("state,outputs,expected", [
    ("queued", [], False), ("failed", [], False), ("succeeded", [], False),
    ("succeeded", [{"name": "sfx", "digest": "sha256:" + "a" * 64, "size": 9644}], False),
    ("succeeded", [{"name": name, "digest": "sha256:" + "a" * 64, "size": 100}
                   for name in ("sfx", "sfx_meta")], True),
])
def test_admission_is_not_task_settlement(monkeypatch, stub_installed_profile_binding, state, outputs, expected):
    def cli(module, argv, name, **kwargs):
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state, "result": {"outputs": outputs}}})
        return json.dumps({"ok": True, "data": {"task_id": "task-1"}})

    monkeypatch.setattr(journey, "cli", cli)
    if expected:
        journey.task()
    else:
        with pytest.raises(RuntimeError, match="did not settle"):
            journey.task()


def test_failed_follow_cancels_and_settles_before_returning_failure(tmp_path, monkeypatch,
                                                                    stub_installed_profile_binding):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    calls = []
    state = "running"
    follow_count = 0

    def cli(module, argv, name, **kwargs):
        nonlocal state, follow_count
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        calls.append(tuple(argv[:2]))
        if argv[:2] == ["projects", "create"]:
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "create"]:
            return json.dumps({"ok": True, "data": {"task_id": "task-1"}})
        if argv[:2] == ["tasks", "follow"]:
            follow_count += 1
            if follow_count == 1:
                raise RuntimeError("task-follow.jsonl: exit 1")
            state = "cancelled"
            raise RuntimeError("task-cancel-follow.jsonl: exit 1")
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state, "result": {"outputs": []}}})
        if argv[:2] == ["tasks", "cancel"]:
            state = "cancel_requested"
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "events"]:
            return json.dumps({"ok": True, "data": []})
        raise AssertionError(argv)

    monkeypatch.setattr(journey, "cli", cli)
    monkeypatch.setattr(journey, "snapshot", lambda name: [process(
        22, 21, "10:02", command=(r"C:\Users\runner\py\Scripts\python.exe -X utf8 -m "
                                   r"astrid.packs.chiptune.actions.sfx.run --out "
                                   r"C:\Users\runner\AppData\Local\Temp\astrid-attempt-task-1-token\outputs")
    )])
    monkeypatch.setattr(journey, "capture_sfx_output", lambda task_id, rows: None)
    with pytest.raises(RuntimeError, match="reached terminal state 'cancelled'"):
        journey.task()

    assert ("tasks", "cancel") in calls
    assert follow_count == 2
    assert calls.index(("tasks", "events")) < calls.index(("tasks", "cancel"))
    assert calls[-2:] == [("tasks", "show"), ("tasks", "events")]


def test_timeout_stream_tails_are_captured_before_cancellation_and_bounded(tmp_path, monkeypatch,
                                                                           stub_installed_profile_binding):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "service-pack-host.log").write_text("host at timeout\n", encoding="utf-8")
    attempt = tmp_path / "astrid-attempt-task-1-token"
    attempt.mkdir()
    stdout = "x" * (journey.TASK_OUTPUT_TAIL_BYTES + 19)
    (attempt / "task-stdout-tail.log").write_text(stdout, encoding="utf-8")
    (attempt / "task-stderr-tail.log").write_text("worker stderr at timeout\n", encoding="utf-8")
    metadata = {
        "schema_version": 1,
        "streams": {
            "stdout": {"file": "task-stdout-tail.log", "available": True,
                       "byte_count": journey.TASK_OUTPUT_TAIL_BYTES, "truncated": True, "error": None},
            "stderr": {"file": "task-stderr-tail.log", "available": True,
                       "byte_count": 26, "truncated": False, "error": "OSError"},
        },
    }
    (attempt / "task-output-capture.json").write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    calls = []
    state = "running"

    def cli(module, argv, name, **kwargs):
        nonlocal state
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        calls.append(tuple(argv[:2]))
        if argv[:2] == ["projects", "create"]:
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "create"]:
            return json.dumps({"ok": True, "data": {"task_id": "task-1"}})
        if argv[:2] == ["tasks", "follow"]:
            raise RuntimeError("follow timed out")
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state}})
        if argv[:2] == ["tasks", "cancel"]:
            assert (tmp_path / "task-timeout-task-stdout-tail.log").read_text(encoding="utf-8") == (
                stdout[-journey.TASK_OUTPUT_TAIL_BYTES:]
            )
            assert (tmp_path / "task-timeout-task-stderr-tail.log").read_text(
                encoding="utf-8",
            ) == "worker stderr at timeout\n"
            evidence = json.loads((tmp_path / "task-logs-before-cancel.json").read_text(encoding="utf-8"))
            assert evidence["task_child_output"]["available"] is True
            assert evidence["task_child_output"]["missing"] == []
            assert evidence["task_child_output"]["metadata"]["status"] == "valid"
            assert evidence["task_child_output"]["metadata"]["streams"]["stderr"]["error"] == "OSError"
            assert evidence["task_child_output"]["streams"] == [
                {"source": "task-stdout-tail.log", "evidence": "task-timeout-task-stdout-tail.log",
                 "bytes": journey.TASK_OUTPUT_TAIL_BYTES, "truncated": True},
                {"source": "task-stderr-tail.log", "evidence": "task-timeout-task-stderr-tail.log",
                 "bytes": len("worker stderr at timeout\n"), "truncated": False},
            ]
            state = "cancelled"
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "events"]:
            return json.dumps({"ok": True, "data": []})
        raise AssertionError(argv)

    monkeypatch.setattr(journey, "cli", cli)
    command = (f"python -m astrid.packs.chiptune.actions.sfx.run --out "
               f"{attempt / 'outputs'}")
    monkeypatch.setattr(journey, "snapshot", lambda name: [process(
        22, 21, "10:02", command=command,
    )])
    monkeypatch.setattr(journey, "capture_sfx_output", lambda task_id, rows: None)
    with pytest.raises(RuntimeError, match="reached terminal state 'cancelled'"):
        journey.task()

    assert calls.index(("tasks", "cancel")) > calls.index(("tasks", "show"))
    assert (tmp_path / "task-timeout-service-pack-host.log").read_text(encoding="utf-8") == "host at timeout\n"
    assert json.loads((tmp_path / "task-timeout-task-output-capture.json").read_text()) == metadata


def test_snapshot_failure_does_not_prevent_cancel_or_hide_follow_failure(tmp_path, monkeypatch,
                                                                        stub_installed_profile_binding):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir()
    calls = []
    state = "running"

    def cli(module, argv, name, **kwargs):
        nonlocal state
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        calls.append(tuple(argv[:2]))
        if tuple(argv[:2]) in (("projects", "create"), ("tasks", "events")):
            return json.dumps({"ok": True, "data": []})
        if argv[:2] == ["tasks", "follow"]:
            raise RuntimeError("follow timed out")
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state}})
        if argv[:2] == ["tasks", "cancel"]:
            manifest = json.loads((tmp_path / "task-logs-before-cancel.json").read_text())
            assert manifest["task_child_output"]["metadata"]["status"] == "missing"
            state = "cancelled"
            return json.dumps({"ok": True})
        raise AssertionError(argv)

    monkeypatch.setattr(journey, "cli", cli)
    monkeypatch.setattr(
        journey, "snapshot", lambda name: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
    )
    with pytest.raises(RuntimeError, match="reached terminal state 'cancelled'"):
        journey.task()

    assert calls.index(("tasks", "cancel")) > calls.index(("tasks", "show"))
    assert (tmp_path / "processes-after-task-follow-error.json").is_file()


def test_malformed_task_metadata_is_reported_without_blocking_cancellation(tmp_path, monkeypatch,
                                                                          stub_installed_profile_binding):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir()
    attempt = tmp_path / "astrid-attempt-task-1-token"
    attempt.mkdir()
    (attempt / "task-output-capture.json").write_text("{broken", encoding="utf-8")
    calls = []
    state = "running"

    def cli(module, argv, name, **kwargs):
        nonlocal state
        if module == "astrid.omp_invoke":
            return sdk_invocation(argv)
        calls.append(tuple(argv[:2]))
        if argv[:2] == ["projects", "create"]:
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "create"]:
            return json.dumps({"ok": True, "data": {"task_id": "task-1"}})
        if argv[:2] == ["tasks", "follow"]:
            raise RuntimeError("follow timed out")
        if argv[:2] == ["tasks", "show"]:
            return json.dumps({"ok": True, "data": {"state": state}})
        if argv[:2] == ["tasks", "cancel"]:
            manifest = json.loads((tmp_path / "task-logs-before-cancel.json").read_text())
            assert manifest["task_child_output"]["metadata"]["status"] == "malformed"
            state = "cancelled"
            return json.dumps({"ok": True})
        if argv[:2] == ["tasks", "events"]:
            return json.dumps({"ok": True, "data": []})
        raise AssertionError(argv)

    command = f"python -m astrid.packs.chiptune.actions.sfx.run --out {attempt / 'outputs'}"
    monkeypatch.setattr(journey, "cli", cli)
    monkeypatch.setattr(journey, "snapshot", lambda name: [process(22, 21, "10:02", command=command)])
    monkeypatch.setattr(journey, "capture_sfx_output", lambda task_id, rows: None)
    with pytest.raises(RuntimeError, match="reached terminal state 'cancelled'"):
        journey.task()

    assert ("tasks", "cancel") in calls
    assert not (tmp_path / "task-timeout-task-output-capture.json").exists()


def test_oversized_task_metadata_is_rejected_with_a_bounded_read(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    attempt = tmp_path / "astrid-attempt-task-1-token"
    attempt.mkdir()
    (attempt / "task-output-capture.json").write_bytes(b"x" * (journey.TASK_OUTPUT_METADATA_BYTES + 100))

    result = journey._task_output_metadata(attempt)

    assert result == {
        "available": False,
        "status": "oversized",
        "source": "task-output-capture.json",
        "bytes": journey.TASK_OUTPUT_METADATA_BYTES + 100,
    }
    assert not (tmp_path / "evidence" / "task-timeout-task-output-capture.json").exists()


def test_sfx_output_capture_restricts_path_to_the_journey_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    command = (r"C:\Users\runner\py\Scripts\python.exe -X utf8 -m "
               r"astrid.packs.chiptune.actions.sfx.run --out "
               r"C:\Users\runner\AppData\Local\Temp\astrid-attempt-task-1-token\outputs")
    journey.capture_sfx_output("task-1", [process(22, 21, "10:02", command=command)])
    evidence = json.loads((tmp_path / "task-sfx-output-evidence.json").read_text())
    assert evidence == {
        "found": True,
        "files": {name: {"exists": False} for name in ("manifest.json", "sfx.json", "sfx.wav")},
    }

    unrelated = command.replace("astrid-attempt-task-1-token", "other-task")
    journey.capture_sfx_output("task-1", [process(22, 21, "10:02", command=unrelated)])
    evidence = json.loads((tmp_path / "task-sfx-output-evidence.json").read_text())
    assert evidence == {"found": False, "files": {}}


def test_collect_preserves_timeout_logs_separately_from_final_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "service-pack-host.log").write_text("host at timeout\n", encoding="utf-8")
    (tmp_path / "logs" / "service-worker.log").write_text("worker at timeout\n", encoding="utf-8")
    attempt = tmp_path / "astrid-attempt-task-1-token"
    attempt.mkdir()
    (attempt / "task-stdout-tail.log").write_text("child at timeout\n", encoding="utf-8")
    (attempt / "task-stderr-tail.log").write_text("child error at timeout\n", encoding="utf-8")
    command = (f"python -m astrid.packs.chiptune.actions.sfx.run --out "
               f"{attempt / 'outputs'}")

    journey.capture_task_logs("task-1", [process(22, 21, "10:02", command=command)])
    (tmp_path / "logs" / "service-pack-host.log").write_text("host after shutdown\n", encoding="utf-8")
    (tmp_path / "logs" / "service-worker.log").write_text("worker after shutdown\n", encoding="utf-8")
    monkeypatch.setattr(journey, "health", lambda: None)
    monkeypatch.setattr(journey, "snapshot", lambda name: [])
    journey.collect()

    evidence = tmp_path / "evidence"
    assert (evidence / "task-timeout-service-pack-host.log").read_text() == "host at timeout\n"
    assert (evidence / "task-timeout-service-worker.log").read_text() == "worker at timeout\n"
    assert (evidence / "task-timeout-task-stdout-tail.log").read_text() == "child at timeout\n"
    assert (evidence / "task-timeout-task-stderr-tail.log").read_text() == "child error at timeout\n"
    assert (evidence / "service-pack-host.log").read_text() == "host after shutdown\n"
    manifest = json.loads((evidence / "task-logs-before-cancel.json").read_text())
    assert manifest["logs"] == [
        {"source": "service-pack-host.log", "evidence": "task-timeout-service-pack-host.log"},
        {"source": "service-worker.log", "evidence": "task-timeout-service-worker.log"},
    ]


def test_timeout_manifest_marks_missing_task_streams(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(journey, "data_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir()

    journey.capture_task_logs("task-1", [])

    manifest = json.loads((tmp_path / "evidence" / "task-logs-before-cancel.json").read_text())
    assert manifest["task_child_output"] == {
        "available": False,
        "streams": [],
        "missing": ["task-stdout-tail.log", "task-stderr-tail.log"],
        "metadata": {
            "available": False,
            "status": "missing",
            "source": "task-output-capture.json",
        },
        "reason": "One or more per-attempt task stream tails were unavailable before cancellation.; "
                  "Task output capture metadata is missing.",
    }


def test_python_stacks_capture_uses_py_spy_for_each_python_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="Thread 0 (active)\n", stderr="")

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.capture_task_python_stacks([
        process(31, 1, "10:00", exe=r"C:\Astrid\python.exe"),
        process(32, 31, "10:01", command=r'"C:\Astrid\python.exe" -m worker'),
        process(33, 31, "10:02", exe=r"C:\Astrid\node.exe"),
    ])

    assert calls == [["py-spy", "dump", "--pid", "31"], ["py-spy", "dump", "--pid", "32"]]
    assert (tmp_path / "task-timeout-pystack-31.txt").read_text() == "Thread 0 (active)\n"
    assert (tmp_path / "task-timeout-pystack-32.txt").read_text() == "Thread 0 (active)\n"
    assert not (tmp_path / "task-timeout-pystack-33.txt").exists()


def test_python_stack_failure_is_saved_and_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 5, stdout="", stderr="Access denied")

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.capture_task_python_stacks([process(41, 1, "10:00", exe=r"C:\Astrid\python.exe")])

    assert (tmp_path / "task-timeout-pystack-41.txt").read_text() == (
        "py-spy dump failed with exit 5\nAccess denied"
    )


def test_python_stack_without_pid_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    journey.capture_task_python_stacks([{"ppid": 1, "created": "10:00", "exe": r"C:\Astrid\python.exe"}])

    assert (tmp_path / "task-timeout-pystack-missing-pid.txt").read_text() == (
        "py-spy dump not run: Python process has invalid pid None\n"
    )


def test_native_failure_keeps_log_and_fails_journey(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    with pytest.raises(RuntimeError, match="exit 7"):
        journey.run([sys.executable, "-c", "import sys; print('native error', file=sys.stderr); sys.exit(7)"],
                    "bootstrap.log")
    assert "native error" in (tmp_path / "bootstrap.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("shell", ["powershell.exe", r"C:\Program Files\PowerShell\7\pwsh.exe"])
def test_powershell_child_rebuilds_module_path_without_mutating_environment(tmp_path, monkeypatch, shell):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setenv("PSModulePath", r"C:\pwsh\Modules;C:\pwsh\7\Modules")
    monkeypatch.setenv("KEEP_ME", "inherited")
    original = dict(journey.os.environ)
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.run([shell, "-NoProfile"], "powershell.log", show=False)

    assert "PSModulePath" not in captured["env"]
    assert captured["env"]["KEEP_ME"] == "inherited"
    assert dict(journey.os.environ) == original


def test_powershell_explicit_environment_is_copied_and_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    inherited = {"PSModulePath": "pwsh-path", "KEEP_ME": "explicit"}
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.run(["pwsh.exe", "-NoProfile"], "pwsh.log", env=inherited, show=False)

    assert captured["env"] == {"KEEP_ME": "explicit"}
    assert inherited == {"PSModulePath": "pwsh-path", "KEEP_ME": "explicit"}


def test_non_powershell_child_keeps_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    inherited = {"PSModulePath": "caller-value", "KEEP_ME": "explicit"}
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    journey.run([sys.executable, "-c", "pass"], "python.log", env=inherited, show=False)

    assert captured["env"] is inherited
    assert inherited == {"PSModulePath": "caller-value", "KEEP_ME": "explicit"}


@pytest.mark.parametrize(
    ("case", "response", "exit_code"),
    [
        ("absent", {"status": "absent", "error": {
            "type": "Microsoft.PowerShell.Cmdletization.Cim.CimJobException",
            "fully_qualified_error_id": "CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask",
            "message": "No matching task", "hresult": -2146233088, "category": "ObjectNotFound"}}, 0),
        ("present", {"status": "present", "tasks": [
            {"path": "\\", "executables": [r"C:\journey\program\python.exe"]}]}, 0),
        ("error", {"status": "error", "error": {
            "type": "System.UnauthorizedAccessException", "fully_qualified_error_id": "AccessDenied",
            "message": "Access denied", "hresult": -2147024891, "category": "PermissionDenied"}}, 1),
    ], ids=["absent", "present", "powershell-error"])
def test_windows_scheduled_task_preflight_classification(tmp_path, monkeypatch, capsys, case, response, exit_code):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    def fake_run(argv, **kwargs):
        assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        script = argv[-1]
        assert "Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop" in script
        assert "CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask" in script
        assert "[System.Management.Automation.ErrorCategory]::ObjectNotFound" in script
        assert "SilentlyContinue" not in script
        assert script.count("exit 0") == 2 and "exit 1" in script
        assert kwargs["capture_output"] and kwargs["timeout"] == 30
        return subprocess.CompletedProcess(argv, exit_code, json.dumps(response).encode(), b"diagnostic stderr")
    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    if case == "error":
        with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
            journey.scheduled_task_preflight("fresh-install")
    else:
        assert journey.scheduled_task_preflight("fresh-install") == response
    evidence = json.loads((tmp_path / "preflight-scheduled-task-fresh-install.json").read_text())
    assert evidence["status"] == case
    assert evidence["observation"] == response
    assert evidence["exit_code"] == exit_code
    assert evidence["stderr"] == "diagnostic stderr"
    line = capsys.readouterr().out.strip()
    prefix = "Scheduled Task preflight (fresh-install): "
    assert line.startswith(prefix)
    assert json.loads(line[len(prefix):]) == evidence


def test_windows_scheduled_task_preflight_rejects_inconsistent_process_result(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    monkeypatch.setattr(journey.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, b'{"status":"absent"}', b"query failed"))
    with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
        journey.scheduled_task_preflight("fresh-install")
    evidence = json.loads((tmp_path / "preflight-scheduled-task-fresh-install.json").read_text())
    assert evidence["status"] == "error"
    assert evidence["protocol_error"] == "Unexpected query result or exit code"
    assert evidence["stderr"] == "query failed"


def test_windows_diagnostics_rejects_failed_origin_even_with_absent_task(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    def run(argv, **kwargs):
        if argv[-1] == journey.SCHEDULED_TASK_PREFLIGHT:
            return subprocess.CompletedProcess(argv, 0, b'{"status":"absent"}', b"")
        return subprocess.CompletedProcess(argv, 1, "", "origin failed")
    monkeypatch.setattr(journey.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="origin exit 1"):
        journey.scheduled_task_diagnostics(leg="fresh-install")
    diagnostic = json.loads((tmp_path / "preflight-scheduled-task.txt").read_text())
    assert diagnostic["origin"]["stderr"] == "origin failed"
    assert diagnostic["query"]["classification"] == "not-found"


def test_windows_preflight_refuses_existing_task_and_allows_only_owned_nonfresh(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    monkeypatch.setattr(shared_journey, "program_root", lambda: tmp_path / "default" / "program")
    monkeypatch.setattr(shared_journey, "runtime_processes", lambda data: [])
    root, data = Path(r"C:\journey\program"), tmp_path / "data"
    observation = {"status": "present", "tasks": [
        {"path": "\\", "executables": [r"C:\journey\program\python.exe"]}]}
    monkeypatch.setattr(journey, "scheduled_task_diagnostics", lambda **kwargs: observation)
    with pytest.raises(RuntimeError, match="outside this journey"):
        shared_journey.assert_host_available("fresh-install", root, data)
    shared_journey.assert_host_available("stale-update", root, data)
    observation["tasks"][0]["executables"].append(r"C:\other\python.exe")
    with pytest.raises(RuntimeError, match="outside this journey"):
        shared_journey.assert_host_available("stale-update", root, data)


def test_windows_journey_success_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    monkeypatch.setattr(shared_journey, "execute", lambda args: {"status": "pass"})
    assert shared_journey.main([
        "--leg", "fresh-install", "--app-checkout", str(tmp_path),
        "--run-root", str(tmp_path / "run"), "--root", str(tmp_path / "run" / "program"),
        "--data-root", str(tmp_path / "run" / "data"), "--shell", "powershell.exe",
    ]) == 0
    monkeypatch.setitem(sys.modules, "ci_journey", shared_journey)
    monkeypatch.setattr(sys, "argv", ["ci_windows_journey.py", "journey", "--leg", "fresh-install",
                                  "--app-checkout", str(tmp_path), "--run-root", str(tmp_path / "run"),
                                  "--root", str(tmp_path / "run" / "program"),
                                  "--data-root", str(tmp_path / "run" / "data")])
    assert journey.main() == 0


@pytest.mark.parametrize("status,exit_code", [("absent", 0), ("present", 0), ("error", 1), ("unknown", 0)])
def test_direct_windows_workflow_preflight_invocation(tmp_path, monkeypatch, capsys, status, exit_code):
    workflow = (WORKFLOW.parent / "journey.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Capture Windows scheduled-task query diagnostics\n", 1)[1].split(
        "      - name: Retain hosted service-manager prerequisite diagnostics\n", 1)[0]
    assert "JOURNEY_LEG: ${{ matrix.leg }}" in step
    assert "if ($null -ne $record.exception -or $record.exit_code -ne 0) { exit 1 }" in step
    code = step.split('          $code = "', 1)[1].split('"\n', 1)[0]
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("JOURNEY_LEG", "release0-to-release1")
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    response = {"status": status}
    if status == "absent":
        # The hosted raw probe returned exit 1 with empty streams for this exact
        # structured no-match result; no separate raw query should decide it.
        response["error"] = {"fully_qualified_error_id": "CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask",
                             "category": "ObjectNotFound"}
    if status == "present":
        response["tasks"] = [{"path": "\\", "executables": [r"C:\journey\python.exe"]}]
    if status == "error":
        response["error"] = {"message": "Access denied"}
    def run(argv, **kwargs):
        if argv[-1].endswith(journey.SCHEDULED_TASK_ORIGIN):
            return subprocess.CompletedProcess(argv, 0, "origin", "")
        assert argv[-1] == journey.SCHEDULED_TASK_PREFLIGHT
        return subprocess.CompletedProcess(argv, exit_code, json.dumps(response).encode(), b"diagnostic stderr")
    monkeypatch.setattr(journey.subprocess, "run", run)
    if status in {"error", "unknown"}:
        with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
            exec(code, {})
    else:
        exec(code, {})
    evidence = json.loads((tmp_path / "journey-manager-direct-windows" /
                           "preflight-scheduled-task-release0-to-release1.json").read_text())
    assert evidence["status"] == ("error" if status == "unknown" else status)
    if status != "unknown":
        assert evidence["observation"] == response
    assert evidence["exit_code"] == exit_code and evidence["stderr"] == "diagnostic stderr"
    diagnostic = json.loads((tmp_path / "journey-manager-direct-windows" / "preflight-scheduled-task.txt").read_text())
    assert diagnostic["query"]["classification"] == {"absent": "not-found", "present": "present"}.get(status, "error")
    prefix = "Scheduled Task preflight (release0-to-release1): "
    assert json.loads(capsys.readouterr().out.strip().removeprefix(prefix)) == evidence


def test_windows_manager_prerequisite_passes_lifecycle_leg(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(journey, "scheduled_task_preflight", lambda leg: calls.append(leg))
    monkeypatch.setattr(journey, "powershell", lambda *args, **kwargs: "5.1")
    args = shared_journey.argparse.Namespace(job_id="direct-windows", mode="direct",
                                            leg="release0-to-release1")
    result = shared_journey.release1_manager_prerequisite(args, tmp_path)
    assert calls == ["release0-to-release1"]
    assert result["outcome"] == "pass" and result["manager"] == "task-scheduler"


def test_windows_scheduled_task_preflight_records_timeout_output(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30, output=b"partial output", stderr=b"partial error")
    monkeypatch.setattr(journey.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
        journey.scheduled_task_preflight("fresh-install")
    evidence = json.loads((tmp_path / "preflight-scheduled-task-fresh-install.json").read_text())
    assert evidence["status"] == "error" and evidence["exit_code"] is None
    assert evidence["stdout"] == "partial output" and evidence["stderr"] == "partial error"
    assert "Traceback" in evidence["process_error"] and "TimeoutExpired:" in evidence["process_error"]


@pytest.mark.parametrize("failure", ["origin", "query"])
def test_combined_windows_preflight_fails_closed_at_either_checkpoint(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(shared_journey.sys, "platform", "win32")
    monkeypatch.setattr(shared_journey, "program_root", lambda: tmp_path / "default" / "program")
    monkeypatch.setattr(shared_journey, "runtime_processes", lambda data: [])
    monkeypatch.setattr(journey, "EVIDENCE", tmp_path / "evidence")
    calls = []
    def run(argv, **kwargs):
        if argv[-1] == journey.SCHEDULED_TASK_PREFLIGHT:
            calls.append("query")
            if failure == "query":
                return subprocess.CompletedProcess(argv, 1, b'{"status":"error"}', b"access denied")
            return subprocess.CompletedProcess(argv, 0, b'{"status":"absent"}', b"")
        calls.append("origin")
        return subprocess.CompletedProcess(argv, 1 if failure == "origin" else 0, "", "")
    monkeypatch.setattr(journey.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="origin exit 1" if failure == "origin" else "Scheduled Task preflight failed"):
        shared_journey.assert_host_available("fresh-install", tmp_path / "program", tmp_path / "data")
    assert calls == ["origin", "query"]
